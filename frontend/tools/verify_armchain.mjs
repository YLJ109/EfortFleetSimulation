// =====================================================================
// 回归测试：华沿 ELFIN 关节链（src/three/armChain.js）的零位高度对拍。
//
// 判据为什么用「站立高度」：它是**独立来源**的数字 —— 官方 README 机型表里
// 逐型号公布的站立高度，跟 xacro 的关节链是两条互不相干的材料。
// 零位时 ELFIN 的手臂是竖直叠起来的，所以
//     本地FK(零位) 的 TCP 高度  ==  standing_height_mm
// 只要 rpy 读反、欧拉序用错、或某个 xyz 抄错，这个数就对不上，
// 而"六台机型全对"基本排除了巧合。
//
// ★ 参数表不复制第二份：直接从 config/models/huayan_elfin.yaml 现读，
//   那份文件又是从 vendor/elfin_model/urdf/*.xacro 逐条抄的。
//   再在本脚本里手抄一遍数值表，就成了"测试自己和自己对拍"，测不出抄错。
//   （node_modules 里的 js-yaml 是 eslint 的传递依赖，随时可能消失，故自己解析。）
//
// 运行： cd frontend && node tools/verify_armchain.mjs
// =====================================================================
import fs from "node:fs";
import path from "node:path";
import * as THREE from "three";
import { buildElfChain } from "../src/three/armChain.js";

const YAML = path.resolve(process.cwd(), "..", "config", "models", "huayan_elfin.yaml");

/** flow 样式（{ name: "J1", xyz: [0, 0, 0.22] }）→ JSON：给裸键补引号即可。 */
function flowToJson(s) {
  return JSON.parse(s.replace(/([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:/g, '$1"$2":'));
}

/**
 * 只解析本脚本要用的字段：variants.<key>.{joints, end_fixed, standing_height_mm}。
 * 解析不到就报错，**绝不静默返回空表** —— 空表会让下面的断言全跳过、测试假绿。
 */
function parseVariants(text) {
  const lines = text.split(/\r?\n/);
  const out = {};
  let inVariants = false;
  let cur = null;
  for (const raw of lines) {
    if (/^variants:\s*$/.test(raw)) { inVariants = true; cur = null; continue; }
    if (inVariants && /^[A-Za-z_]/.test(raw)) break;              // 顶格键 = variants 结束
    if (!inVariants || !raw.trim() || raw.trim().startsWith("#")) continue;
    const key = raw.match(/^ {2}([a-z0-9_]+):\s*$/);
    if (key) { cur = out[key[1]] = { joints: [] }; continue; }
    if (!cur) continue;
    const joint = raw.match(/^\s*-\s*\{\s*name:\s*"(J\d)"[\s\S]*?\}\s*$/);
    if (joint) { cur.joints.push(flowToJson(raw.trim().replace(/^-\s*/, ""))); continue; }
    const end = raw.match(/^ {4}end_fixed:\s*(\{.*\})/);
    if (end) { cur.end_fixed = flowToJson(end[1]); continue; }
    const h = raw.match(/^ {4}standing_height_mm:\s*([0-9.]+)/);
    if (h) { cur.standing_height_mm = Number(h[1]); continue; }
    const r = raw.match(/^ {4}reach_mm:\s*([0-9.]+)/);
    if (r) { cur.reach_mm = Number(r[1]); }
  }
  return out;
}

let fail = 0;
function check(ok, msg) {
  if (!ok) fail++;
  console.log(`  ${ok ? "PASS" : "FAIL"}  ${msg}`);
}

const text = fs.readFileSync(YAML, "utf8");
const V = parseVariants(text);
const names = Object.keys(V);
console.log(`读取机型档案: ${path.relative(process.cwd(), YAML).split(path.sep).join("/")}`);
console.log(`\n=== 0) 解析自检（解析器别悄悄返回空表） ===`);
check(names.length === 6, `解析到 6 个机型: ${names.join(", ")}`);
for (const n of names) {
  const v = V[n] || {};
  check(v.joints?.length === 6 && v.end_fixed && Number.isFinite(v.standing_height_mm),
    `${n}: 6 轴 + end_fixed + standing_height_mm 齐全`);
}
if (fail) {
  console.log(`\n===== 结果: ${fail} 项失败（解析阶段就挂了，后面没跑） =====`);
  process.exit(1);
}

// ---------------------------------------------------------------------
console.log(`\n=== 1) 零位 TCP 高度 == 官方站立高度（判 rpy 顺序 / 欧拉序 / xyz 有没有抄错） ===`);
for (const n of names) {
  const v = V[n];
  const m = buildElfChain(v);
  m.root.updateMatrixWorld(true);
  const t = m.getTcp();                      // 场景世界坐标, mm（根节点已把 URDF +Z 转成 Three +Y）
  const expectH = v.standing_height_mm;
  const dh = Math.abs(t.y - expectH);
  const horiz = Math.hypot(t.x, t.z);        // 零位是竖直叠臂 → 水平偏移应≈0
  const expectStr = `${n}  零位TCP=(${t.x.toFixed(1)}, ${t.y.toFixed(1)}, ${t.z.toFixed(1)})mm  ` +
    `官方站立高度=${expectH}mm  高差=${dh.toFixed(2)}mm  水平偏移=${horiz.toFixed(2)}mm`;
  check(dh <= 1.0 && horiz <= 1.0, expectStr);
}

// ---------------------------------------------------------------------
console.log(`\n=== 2) 反向验证：把 rpy 当成 (yaw,pitch,roll) 读会崩（证明上面那条判据真的有灵敏度） ===`);
{
  const v = V.elfin5;
  const swapped = { ...v, joints: v.joints.map((j) => ({ ...j, rpy: [j.rpy[2], j.rpy[1], j.rpy[0]] })) };
  const bad = buildElfChain(swapped);
  bad.root.updateMatrixWorld(true);
  const t = bad.getTcp();
  const d = Math.abs(t.y - v.standing_height_mm);
  check(d > 100, `误读顺序后 TCP 高 ${t.y.toFixed(1)}mm，与站立高度差 ${d.toFixed(1)}mm（>100mm 才算判据有效）`);
}

// ---------------------------------------------------------------------
console.log(`\n=== 3) 关节真的驱动连杆（只动 J2，末端必须前后倒；否则转动层挂错了地方） ===`);
{
  const v = V.elfin5;
  const m = buildElfChain(v);
  m.root.updateMatrixWorld(true);
  const before = m.getTcp();
  check(m.applyJoints([0, 30, 0, 0, 0, 0]) === true, `applyJoints([0,30,...]) 接受合法帧`);
  m.root.updateMatrixWorld(true);
  const after = m.getTcp();
  const moved = Math.hypot(after.x - before.x, after.y - before.y, after.z - before.z);
  // J2 的 rpy 把转动轴转到**基座 -Y**（水平），所以摆 J2 = 手臂在基座 X-Z 平面内前后倒，
  // 末端高度必然下降、基座 Y 方向（= Three 的 -z）必须保持 0。
  // 若这里 z 也跟着动了，说明转动层挂错了坐标系（比如挂到了静态偏置层 link 上）。
  // 阈值取 0.01mm 而不是 0：xacro 原文把 π/2 写成 1.5708（四位小数），
  // 轴位因此差 1.8e-5 rad，摆 30° 会残留微米级侧向分量 —— 那是**抄录精度**，不是接错。
  const sideways = Math.abs(after.z - before.z);
  check(moved > 100, `J2=+30° 末端位移 ${moved.toFixed(1)}mm`);
  check(after.y < before.y - 100, `J2=+30° 末端高度 ${before.y.toFixed(1)}→${after.y.toFixed(1)}mm（前后倒，降低）`);
  check(sideways < 0.01, `J2 摆动不离开基座 X-Z 平面（侧向泄漏 ${sideways.toExponential(1)}mm，鼓轴没接错）`);
}

// ---------------------------------------------------------------------
console.log(`\n=== 4) 坏帧与荒谬值：坏帧整帧丢弃且姿态不动，±360° 之外夹住 ===`);
{
  const m = buildElfChain(V.elfin5);
  m.applyJoints([10, 20, 30, 40, 50, 60]);
  m.root.updateMatrixWorld(true);
  const keep = m.getTcp();
  check(m.applyJoints([10, NaN, 30, 40, 50, 60]) === false, `含 NaN 的帧返回 false（丢弃）`);
  m.root.updateMatrixWorld(true);
  const same = m.getTcp();
  check(Math.hypot(same.x - keep.x, same.y - keep.y, same.z - keep.z) < 1e-9,
    `丢弃坏帧后姿态保持上一帧（不会甩臂回零位）`);
  check(m.applyJoints([null, undefined, 1e12, "abc", -1e12, 0]) === false,
    `null/undefined/字符串/无穷大都算坏帧`);
  check(m.applyJoints([720, -720, 0, 0, 0, 0]) === true, `±720° 接受（但应被夹到 ±360）`);
  check(Math.abs(m.joints[0].rotation.z - Math.PI * 2) < 1e-9,
    `720° 被夹到 360°（不可能是真机主值区间的读数）`);
}

// ---------------------------------------------------------------------
console.log(`\n=== 5) 12 台共用的前提：材质可注入 + 归因可用（jointOwner 认这条链） ===`);
{
  const shared = { body: null };
  const mk = () => new THREE.MeshStandardMaterial({ color: 0xabcdef });
  shared.body = mk(); shared.joint = mk(); shared.hard = mk(); shared.metal = mk();
  const a = buildElfChain(V.elfin5, shared);
  const b = buildElfChain(V.elfin5, shared);
  const used = new Set();
  a.root.traverse((o) => { if (o.isMesh) used.add(o.material); });
  b.root.traverse((o) => { if (o.isMesh) used.add(o.material); });
  check(used.size === 4, `两台共用 4 个材质实例，不是 8 个（实际 ${used.size}）`);
  const b2 = buildElfChain(V.elfin5);          // 不传材质 → 自建
  const own = new Set();
  b2.root.traverse((o) => { if (o.isMesh) own.add(o.material); });
  check(!own.has(shared.body), `默认材质与注入材质不是同一批对象`);

  const j1 = a.root.getObjectByName("J1");
  const j6 = a.root.getObjectByName("J6");
  check(!!j1 && !!j6, `J1..J6 组按名可取（官方 GLB 那条链同名，摆放代码一套通用）`);
  check(!!a.flangeNode && a.flangeNode.name === "Flange", `法兰挂载点存在且名为 Flange（末端工具挂这里，不硬编码 z）`);
  let meshCount = 0;
  a.root.traverse((o) => { if (o.isMesh) meshCount++; });
  check(meshCount <= 16, `单台 ${meshCount} 个网格 —— 12 台是 ${meshCount * 12} 个，几何必须靠 clone() 共享`);
}

// ---------------------------------------------------------------------
console.log(`\n=== 6) 缺参数时报错要能指到地方（前端拿到半截 kinematics 不能画个怪手臂） ===`);
{
  const cases = [["无 urdf 块", undefined], ["轴数不对", { joints: [{ xyz: [0, 0, 1], rpy: [0, 0, 0] }] }]];
  for (const [label, prof] of cases) {
    let msg = "";
    try { buildElfChain(prof); } catch (e) { msg = String(e && e.message || e); }
    check(msg.includes("6 轴"), `${label} → 抛错并写明需要 6 轴: "${msg.slice(0, 46)}"`);
  }
}

// ---------------------------------------------------------------------
console.log(`\n=== 7) 连杆必须真的接到下一个关节（"脱骨"回归钉子） ===`);
{
  // 现场翻车过一次：linkTo 里 CylinderGeometry 的轴是局部 +Y，却按 +Z 去对齐方向，
  // 于是每根杆横着躺、够不到下一个关节，画出来六节鼓各自悬空。
  // 光看"零位 TCP 高度对不对"抓不到它 —— 高度是 FK 算的，跟杆子怎么画无关。
  // 所以这里直接量**每根杆的两个端面**与相邻关节原点的距离。
  let checked = 0;
  for (const name of names) {
    const c = buildElfChain(V[name]);
    c.root.updateMatrixWorld(true);
    const W = (o) => new THREE.Vector3().setFromMatrixPosition(o.matrixWorld);
    const J = [];
    for (let i = 1; i <= 6; i++) J.push(c.root.getObjectByName("J" + i));
    for (let i = 0; i < 6; i++) {
      const bar = J[i].children.find((o) => o.name === "link-bar");
      if (!bar) continue;                       // 相邻两关节零平移 → 本来就不该有杆
      const nxt = i < 5 ? W(J[i + 1]) : W(c.flangeNode);
      // 半高必须按**包围盒**取：boundingSphere.radius 是圆柱角点半径 sqrt(r²+(h/2)²)，
      // 比半高大一截，用它量会凭空多出几毫米误差（我第一版就栽在这里，差点去"修"几何）。
      bar.geometry.computeBoundingBox();
      const half = (bar.geometry.boundingBox.max.y - bar.geometry.boundingBox.min.y) / 2;
      const up = new THREE.Vector3(0, 1, 0).applyQuaternion(
        bar.getWorldQuaternion(new THREE.Quaternion()));
      const p = W(bar);
      const e1 = p.clone().addScaledVector(up, -half);
      const e2 = p.clone().addScaledVector(up, half);
      const err = Math.min(e1.distanceTo(nxt), e2.distanceTo(nxt));
      check(err < 1e-3, `${name} J${i + 1} 的杆端到下一关节误差 ${err.toFixed(5)}m（>0 就是脱骨）`);
      checked++;
    }
  }
  check(checked >= 6, `量到 ${checked} 根连杆（少于 6 = 各变体都没建出杆，测试本身失效）`);
}

console.log(`\n===== 结果: ${fail === 0 ? "全部通过" : fail + " 项失败"} =====`);
process.exit(fail === 0 ? 0 : 1);
