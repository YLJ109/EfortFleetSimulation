// =====================================================================
// 离地间隙实测工具（离线、headless，用官方 GLB 的真实顶点）
//
// 用途：
//   1) 校核 utils/dance.js 的演示轨迹 —— 最低点不能穿地（<0 就是穿地）。
//   2) 给"地面碰撞"阈值挑一个合理的默认值（接近/危险/碰撞 mm）。
//   3) 换姿态/换模型（程序化 vs 官方）后，快速看出最低点分布。
//
// 运行： node tools/measure_clearance.mjs
//
// ★ 这里用的是**官方数模**（public/models/robot_full.glb）逐顶点求 AABB，
//   与浏览器里 getGroundRoot()（J1 连杆子树）取的包围盒口径一致。
// =====================================================================
import * as THREE from "three";
import fs from "node:fs";

const buf = fs.readFileSync(new URL("../public/models/robot_full.glb", import.meta.url));
let js = null, binOff = -1;
{
  let off = 12;
  while (off < buf.length) {
    const len = buf.readUInt32LE(off), type = buf.readUInt32LE(off + 4);
    if (type === 0x4e4f534a) js = JSON.parse(buf.slice(off + 8, off + 8 + len).toString("utf8"));
    else if (type === 0x004e4942) binOff = off + 8;
    off += 8 + len;
  }
}
const COMP = { 5120: Int8Array, 5121: Uint8Array, 5122: Int16Array, 5123: Uint16Array, 5125: Uint32Array, 5126: Float32Array };
function accessor(aid) {
  const a = js.accessors[aid], bv = js.bufferViews[a.bufferView];
  const start = binOff + (bv.byteOffset || 0) + (a.byteOffset || 0);
  const stride = bv.byteStride || 0, n = a.count, comp = COMP[a.componentType];
  const out = new Float32Array(n * 3);
  if (!stride || stride === 12) {
    const raw = new comp(buf.buffer, buf.byteOffset + start, n * 3);
    for (let i = 0; i < n * 3; i++) out[i] = raw[i];
  } else {
    for (let i = 0; i < n; i++) {
      const s = start + i * stride;
      for (let k = 0; k < 3; k++) out[i * 3 + k] = new comp(buf.buffer, buf.byteOffset + s + k * 4, 1)[0];
    }
  }
  return out;
}
function meshGeo(mi) {
  const prim = js.meshes[mi].primitives[0];
  const pos = accessor(prim.attributes.POSITION);
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
  if (prim.indices !== undefined) {
    const ia = js.accessors[prim.indices];
    const idx = new COMP[ia.componentType](
      buf.buffer, buf.byteOffset + binOff + (js.bufferViews[ia.bufferView].byteOffset || 0) + (ia.byteOffset || 0), ia.count
    );
    g.setIndex(new THREE.BufferAttribute(new Uint32Array(idx), 1));
  }
  g.computeBoundingBox();
  return g;
}

// ---- DH（与 backend config/robot.yaml 同源，单位 mm / deg） ----
const d1 = 376.0, a1 = 49.5932, a2 = 330.1834, a3 = 40.2714, d4 = 329.2414;
const zJ2 = d1, zJ3 = d1 + a2, zJ4 = zJ3 + a3, xJ4 = a1, xJ5 = a1 + d4;
const NAME2LINK = { "转座": 1, "大臂": 2, "手腕体": 4, "手腕": 5, "法兰": 6 };
function fix(s) { try { return Buffer.from(s, "latin1").toString("gbk").split("^")[0]; } catch { return s; } }

const parts = [];
for (const n of js.nodes) {
  if (n.mesh === undefined) continue;
  const g = meshGeo(n.mesh); const bb = g.boundingBox;
  parts.push({
    name: fix(n.name || ""), g,
    c: bb.getCenter(new THREE.Vector3()), maxZ: bb.max.z,
    maxDim: Math.max(bb.max.x - bb.min.x, bb.max.y - bb.min.y, bb.max.z - bb.min.z),
  });
}
function classify(p) {
  if (p.maxDim > 900) return -1;
  if (NAME2LINK[p.name] !== undefined) return NAME2LINK[p.name];
  if (p.maxZ <= zJ2 * 0.70) return 0;
  if (p.maxZ <= zJ2 * 1.25) return 1;
  if (p.c.x >= xJ5 - 20) return p.c.x >= xJ5 + 40 ? 6 : 5;
  if (p.c.z < zJ3) return 2;
  if (p.c.z < zJ4 && p.c.x < xJ4 + 120) return 3;
  return 4;
}

const deg2rad = (d) => (d * Math.PI) / 180;
const scene = new THREE.Scene();
const root = new THREE.Group();
root.rotation.x = -Math.PI / 2; root.scale.setScalar(0.001); scene.add(root);
const joints = []; let parent = root;
// theta_offset：与 manager.buildArticulatedOfficial 一致（J2 偏 90°）
const OFFS = [0, 90, 0, 0, 0, 0];
[[d1, a1, 90], [0.2852, a2, 0], [0, a3, 90], [d4, 0, -90], [0, 0, 90], [80, 0, 0]]
  .forEach(([d, a, al]) => {
    const jg = new THREE.Group(); parent.add(jg);
    const dz = new THREE.Group(); dz.position.z = d; jg.add(dz);
    const ax = new THREE.Group(); ax.position.x = a; dz.add(ax);
    const rx = new THREE.Group(); rx.rotation.x = deg2rad(al); ax.add(rx);
    joints.push(jg); parent = rx;
  });

const glbRoot = new THREE.Group();
glbRoot.rotation.x = -Math.PI / 2; glbRoot.scale.setScalar(0.001); scene.add(glbRoot);
// ★★ attach() 前先把链条摆到**机械零位（含 theta_offset）**，与 manager.js
//    buildArticulatedOfficial 一致。少了这一步，零件被补偿进"没加 J2 偏置"的局部系，
//    q=0 时整具模型是"大臂横着、小臂竖着"（与真实零位差 90°），量出来的最低点全是错的
//    —— 这条已在 tools/dump_mesh_lowpoints.mjs 的注释里记过，两个工具必须同一个姿势。
const nodes = [root, joints[0], joints[1], joints[2], joints[3], joints[4], joints[5]];
applyJoints([0, 0, 0, 0, 0, 0]);
root.updateMatrixWorld(true);
parts.forEach((p) => {
  const k = classify(p);
  if (k < 0) return;
  glbRoot.add(new THREE.Mesh(p.g));
  nodes[k].attach(glbRoot.children[glbRoot.children.length - 1]);
});
glbRoot.visible = false;

function applyJoints(q) {
  for (let i = 0; i < 6; i++) joints[i].rotation.z = deg2rad((q[i] || 0) + OFFS[i]);
}
/** J1 连杆子树（不含贴地底座）最低点，mm。= 浏览器 getGroundRoot() 的口径 */
function minY(q) {
  applyJoints(q); root.updateMatrixWorld(true);
  return new THREE.Box3().setFromObject(joints[0]).min.y * 1000;
}

const P = 8;
/** 候选轨迹：只差 J2/J3 的偏置与摆幅，其余通道固定。 */
function make(A, B, C, D) {
  return (t) => {
    const w = 2 * Math.PI * (t % P) / P;
    return [
      Math.sin(w) * 60,                    // J1 摆头
      A + Math.sin(w * 2) * B,             // J2 抬臂（偏置 A、摆幅 B）
      C + Math.sin(w * 2 + Math.PI) * D,   // J3 肘部（与 J2 反相）
      Math.sin(w * 1.5) * 120,             // J4 翻腕
      Math.sin(w) * 45,                    // J5 点头
      Math.sin(w * 0.5) * 180,             // J6 摆动（平滑，无 360° 回绕）
    ];
  };
}

const cands = [
  ["旧版(穿地)", make(30, 25, 45, 40)],
  ["A", make(-40, 20, 0, 40)],
  ["B", make(-30, 20, 0, 40)],
  ["C", make(-25, 20, 0, 40)],
  ["D", make(-20, 20, 0, 40)],
  ["E", make(-20, 20, 0, 45)],
  ["F", make(-15, 20, 0, 45)],
  ["G", make(-15, 18, 5, 45)],
  ["H", make(-25, 22, 0, 45)],
];

console.log("地面阈值默认 接近150 / 危险80 / 碰撞30 mm");
console.log("候选轨迹一周期内 J1 子树最低点：\n");
console.log("  名称".padEnd(14) + "最低点(mm)".padStart(11) + "   评价");
for (const [name, f] of cands) {
  let worst = Infinity, wq = null;
  for (let t = 0; t <= P; t += 0.04) {
    const q = f(t), y = minY(q);
    if (y < worst) { worst = y; wq = q; }
  }
  let tag;
  if (worst < 0) tag = "✗ 穿地";
  else if (worst <= 80) tag = "✗ 进危险红区";
  else if (worst <= 150) tag = "○ 落黄区（能演示提前提醒）";
  else tag = "· 全程绿（演示看不到变色）";
  console.log("  " + name.padEnd(12) + worst.toFixed(1).padStart(11) + "   " + tag);
  if (worst < 0) console.log("     最低点姿态 q=[" + wq.map((v) => v.toFixed(0)).join(",") + "]");
}

// 参考：零位与几个典型姿态
console.log("\n参考：");
for (const [q, tag] of [
  [[0, 0, 0, 0, 0, 0], "零位"],
  [[0, -40, 0, 0, 0, 0], "J2=-40"],
  [[0, -25, 0, 0, 0, 0], "J2=-25"],
  [[0, -15, 0, 0, 0, 0], "J2=-15"],
  [[0, 0, 45, 0, 0, 0], "J3=45"],
]) {
  console.log("  " + tag.padEnd(12) + "(" + q.join(",") + ") → " + minY(q).toFixed(1) + " mm");
}
