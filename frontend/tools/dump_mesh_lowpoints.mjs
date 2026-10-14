// =====================================================================
// 官方数模逐姿态真值采样器 —— 给后端 safety_eval 的胶囊包络当**标定基准**。
//
// 为什么需要它（第六轮）：
//   A1 服务端独立评估把机器人本体近似成 6 段胶囊。半径如果拍脑袋，净空读数就是猜的
//   —— 正是本项目最反对的"假显示"（实测：猜的包络零位最低点 261mm，官方数模真实值
//   191.5mm → **猜的比真实更高，根本不保守**，会把"快撞台面"判成安全）。
//   `public/models/robot_full.glb` 是官方 STEP 数模 1:1 导出，可以逐顶点量出真值。
//
// 导出两样东西（后端的拟合与回归钉都只吃这两样）：
//   1) rows[]：每个采样姿态下 **每根连杆（class 1..6）的世界包围盒**(mm) 与
//      J1 子树整体最低点。后端据此反解"每段胶囊至少要多大半径才不会漏包"。
//   2) link_local[]：在**关节局部系**（= DH 帧，段的两端都固定在该系里）里量到的
//      垂直半径与轴向伸展，用来核对拟合结果是否合理。
//
// 口径（与后端严格对齐，别自己再加减）：
//   世界系 = 场景系(米)去掉 lift：y 向上、基座平面 y=0；
//   导出的 mm 值满足  y_mm = 基座系 z，x_mm = 基座系 x，z_mm = −基座系 y
//   —— 与后端 `scene_from_base_mm(x,y,z, lift=0) × 1000` 逐分量相同。
//
// 运行： node tools/dump_mesh_lowpoints.mjs   → tools/fixtures/mesh_lowpoints.json
// =====================================================================
import * as THREE from "three";
import fs from "node:fs";
import path from "node:path";

const GLB = new URL("../public/models/robot_full.glb", import.meta.url);
const OUT = path.resolve("tools/fixtures/mesh_lowpoints.json");

const buf = fs.readFileSync(GLB);
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

// ---- DH（与 backend config/robot.yaml 的 dh 段同源，mm / deg）----
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
const root = new THREE.Group();
root.rotation.x = -Math.PI / 2; root.scale.setScalar(0.001);
// joints[i] = 第 i+1 轴转动群；其原点 = DH frames[i]（joints[0] 原点 = frames[0] = 基座）
const joints = []; const frameG = []; let parent = root;
const OFFS = [0, 90, 0, 0, 0, 0];
[[d1, a1, 90], [0.2852, a2, 0], [0, a3, 90], [d4, 0, -90], [0, 0, 90], [80, 0, 0]]
  .forEach(([d, a, al]) => {
    const jg = new THREE.Group(); parent.add(jg);
    const dz = new THREE.Group(); dz.position.z = d; jg.add(dz);
    const ax = new THREE.Group(); ax.position.x = a; dz.add(ax);
    const rx = new THREE.Group(); rx.rotation.x = deg2rad(al); ax.add(rx);
    joints.push(jg); frameG.push(rx); parent = rx;
  });
const glbRoot = new THREE.Group();
glbRoot.rotation.x = -Math.PI / 2; glbRoot.scale.setScalar(0.001);
// ★★ attach() 之前必须先把链条摆到**机械零位（含 theta_offset）**——与 manager.js
//    buildArticulatedOfficial 完全一致（它在 attach 前调 applyJoints([0,0,0,0,0,0])）。
//    漏了这一步 = 零件被补偿进"没加 J2 偏置"的局部系里，q=0 采样到的整具模型
//    大臂横着、小臂竖着（与真实零位差 90°），量出来的最低点/外廓全是错的。
applyJoints([0, 0, 0, 0, 0, 0]);
root.updateMatrixWorld(true);
// class k 的刚体挂在 joints[k-1] 上（k=0 挂 root）⇒ 与 DH frames 的父子关系一致
const nodes = [root, joints[0], joints[1], joints[2], joints[3], joints[4], joints[5]];
const byClass = {};                       // class → [Mesh]
parts.forEach((p) => {
  const k = classify(p);
  if (k < 0) return;
  glbRoot.add(new THREE.Mesh(p.g));
  const m = glbRoot.children[glbRoot.children.length - 1];
  nodes[k].attach(m);
  (byClass[k] = byClass[k] || []).push(m);
});

function applyJoints(q) {
  for (let i = 0; i < 6; i++) joints[i].rotation.z = deg2rad((q[i] || 0) + OFFS[i]);
}

/** class >= 1 的并集最低点（相对基座平面，mm）= 后端 ground 口径 */
function subtreeLow() {
  const u = new THREE.Box3(); let hit = false;
  for (let k = 1; k <= 6; k++) {
    for (const m of byClass[k] || []) {
      m.updateWorldMatrix(true, false);
      const b = new THREE.Box3().setFromObject(m);
      if (!hit) { u.copy(b); hit = true; } else { u.union(b); }
    }
  }
  return hit ? u.min.y * 1000 : Infinity;
}
const _b = new THREE.Box3(), _v = new THREE.Vector3();
/** class 刚体的世界包围盒（mm）；y=0 即基座平面，与后端 lift=0 的 scene_from_base_mm 同口径 */
function classBoxMm(cls) {
  const u = new THREE.Box3(); let hit = false;
  for (const m of byClass[cls] || []) {
    m.updateWorldMatrix(true, false);
    const b = new THREE.Box3().setFromObject(m);
    if (!hit) { u.copy(b); hit = true; } else { u.union(b); }
  }
  if (!hit) return null;
  const r = (v) => Math.round(v * 10000) / 10;      // 米 → 0.1mm
  return [r(u.min.x), r(u.max.x), r(u.min.y), r(u.max.y), r(u.min.z), r(u.max.z)];
}

// ---- 关节局部系里的几何（核对用）----
// 段 i 的两端 = frames[i]（= joints[i] 原点，局部 (0,0,0)）与 frames[i+1]（= joints[i+1] 原点）；
// 该段的刚体 = class i+1（正好挂在 joints[i] 上）。三者都在 joints[i] 局部系里固定。
function linkLocal() {
  applyJoints([0, 0, 0, 0, 0, 0]);
  root.updateMatrixWorld(true);
  const out = [];
  for (let i = 0; i < 6; i++) {
    const ms = byClass[i + 1] || [];
    if (!ms.length) { out.push({ segment: i, meshes: 0 }); continue; }
    const P0 = new THREE.Vector3(0, 0, 0);
    // joints[i] 原点 = frames[i]，frameG[i]（第 i 个 Rx 群）原点 = frames[i+1]
    const P1 = joints[i].worldToLocal(frameG[i].getWorldPosition(new THREE.Vector3()));
    const seg = P1.clone().sub(P0); const L = seg.length(); const dir = seg.clone().normalize();
    const inv = new THREE.Matrix4().copy(joints[i].matrixWorld).invert();
    let rPerp = 0, tMin = Infinity, tMax = -Infinity;
    for (const m of ms) {
      const mat = new THREE.Matrix4().multiplyMatrices(inv, m.matrixWorld);
      const pos = m.geometry.attributes.position;
      for (let k = 0; k < pos.count; k++) {
        _v.fromBufferAttribute(pos, k).applyMatrix4(mat);
        const t = _v.dot(dir);
        const perp = _v.clone().sub(dir.clone().multiplyScalar(t)).length();
        if (perp > rPerp) rPerp = perp;
        if (t < tMin) tMin = t; if (t > tMax) tMax = t;
      }
    }
    // joints[i] 局部系里 1 单位 = 1mm（数模本身是 mm；root 的 0.001 被 attach 抵消）
    const mmv = (v) => Math.round(v * 10) / 10;
    out.push({ segment: i, seg_len_mm: mmv(L),
               r_perp_mm: mmv(rPerp),
               axial_min_mm: mmv(tMin),
               axial_max_mm: mmv(tMax), meshes: ms.length });
  }
  return out;
}

// ---- 采样姿态（限位内工作域 + 故意压低的极端姿态；种子固定 ⇒ 可复现）----
const LIM = [[-150, 150], [-90, 90], [-150, 150], [-180, 180], [-120, 120], [-327, 327]];
function rnd(seed) { let s = seed; return () => { s = (s * 1103515245 + 12345) & 0x7fffffff; return s / 0x7fffffff; }; }
const R = rnd(20261012);
const poses = [];
for (let i = 0; i < 260; i++) poses.push(LIM.map(([a, b]) => Math.round(a + (b - a) * R())));
for (const j2 of [-90, -70, -50, -30, 0, 30])
  for (const j3 of [-150, -90, -40, 0, 40, 90])
    for (const j5 of [-120, -60, 0, 60, 120])
      poses.push([0, j2, j3, 0, j5, 0]);
// 再补一批"手臂完全压低贴台面"的姿态（胶囊最容易被估松的情形）
for (const j1 of [-150, -90, -45, 0, 45, 90, 150])
  for (const j2 of [-90, -80, -70])
    for (const j3 of [-150, -120, -90])
      poses.push([j1, j2, j3, 0, 0, 0]);

const rows = [];
for (const q of poses) {
  applyJoints(q); root.updateMatrixWorld(true);
  const bodies = {};
  for (let k = 0; k <= 6; k++) { const b = classBoxMm(k); if (b) bodies[k] = b; }
  let mnx = Infinity, mxx = -Infinity, mny = Infinity, mxy = -Infinity,
      mnz = Infinity, mxz = -Infinity;
  for (const b of Object.values(bodies)) {
    if (b[0] < mnx) mnx = b[0]; if (b[1] > mxx) mxx = b[1];
    if (b[2] < mny) mny = b[2]; if (b[3] > mxy) mxy = b[3];
    if (b[4] < mnz) mnz = b[4]; if (b[5] > mxz) mxz = b[5];
  }
  const low = subtreeLow();
  if (!Number.isFinite(low)) throw new Error("数模分件异常：J1 子树没有任何 mesh");
  rows.push({ q, low_mm: Math.round(low * 10) / 10,
              aabb: [mnx, mxx, mny, mxy, mnz, mxz], bodies });
}

fs.mkdirSync(path.dirname(OUT), { recursive: true });
fs.writeFileSync(OUT, JSON.stringify({
  generated_by: "tools/dump_mesh_lowpoints.mjs",
  source: "public/models/robot_full.glb（官方 STEP 数模 1:1 导出）",
  convention: "单位 mm；世界系 = 场景系去 lift：y 向上、基座平面 y=0；"
    + "与后端 scene_from_base_mm(x,y,z, lift=0)×1000 逐分量相同 (x, z, -y)",
  aabb_field: "[min_x, max_x, min_y, max_y, min_z, max_z]",
  link_local: linkLocal(),
  count: rows.length,
  rows,
}, null, 1), "utf8");

const lows = rows.map((r) => r.low_mm);
console.log(`写出 ${rows.length} 个姿态 → ${path.relative(process.cwd(), OUT)}`);
console.log("J1 子树最低点(mm)：", Math.min(...lows).toFixed(1), "~", Math.max(...lows).toFixed(1));
console.log("\n段局部几何（核对拟合是否合理）：");
for (const l of linkLocal()) console.log("  " + JSON.stringify(l));
