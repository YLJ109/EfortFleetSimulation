// =====================================================================
// 工作间地面：环氧自流平地坪 + 工程网格 + 机器人接触阴影 + 黄色地标线。
//
// 只负责"地"与"设备基座"，安全围栏在 three/safety.js（可配置、可热重建）。
// 本文件用 CanvasTexture（环氧地坪/黄黑斜纹/接触阴影）。headless 测试里给 `document`
// 打一个最小桩即可正常 import 并建出几何（见 tools/verify_cell.mjs 顶部）。
//
// 地坪做法（纯程序化，无外部贴图）：
//   1) CanvasTexture 画"一块 0.6m 板"：底色 + 云斑 + 骨料斑点 + 深色板缝，
//      按 repeat 平铺 → 像真实环氧地坪的板块拼缝，而不是一块死平的色板
//   2) MeshPhysicalMaterial + clearcoat → 抛光面反光（吃 RoomEnvironment 环境贴图）
//   3) 机器人下方一块软接触阴影，四周 3.6m 处一圈黄色地标线（工业地面分区线）
//   ※ 这一层是"工作区地坪"，铺在 three/lab.js 的整间房地面(14m)之上。
//
// 地板尺寸按现场工作间取 3.6m：机器人(0.81m)置于其中，围栏(1.64m 见方)
// 只占约 45% 画面，避免"模型显得过大"的错觉。
// =====================================================================
import * as THREE from "three";

export const FLOOR_SIZE = 3.6;
export const GRID_STEP = 0.1;
export const TILE_STEP = 0.6;     // 环氧地坪大板缝
// ★ 机器人站台：设备基座。机器人底座装在这块站台上，**立足面 = 台面**。
//   manager 会把机器人抬到 PLATFORM_H；地面碰撞检测也以台面为零点（见 safety.update 的 refFloorY）。
//   ★ 高度取 0.18m（而不是 0.12）：12cm 在 14m 的实验室里几乎看不出来，机器人像直接蹲在地上。
//     18cm + 黄黑警示斜纹带 + 踢脚底盘 + 调平脚 → 一眼就能认出"设备基座"。
//   ★ 尺寸上限受安全围栏约束：站台半宽 + 围栏角柱内缘(半宽0.82 − 柱半宽0.05 = 0.77) 要留出余量。
//     当前台面 1.31/2 = 0.655，与角柱内缘留 11.5cm 间隙，不会顶到柱子。
export const PLATFORM_H = 0.18;
export const PLATFORM_SIZE = 1.24;
// 拉丝台面板的实际边长（比台身外挑 7cm）。报警垫按这个尺寸铺，才能**铺满**整个台面。
export const PLATFORM_TOP_SIZE = PLATFORM_SIZE + 0.07;

/** 一块环氧地坪板（TILE_STEP 见方）的程序化贴图：底色 + 云斑 + 斑点 + 板缝。 */
function makeEpoxyTexture() {
  const N = 256;
  const c = document.createElement("canvas");
  c.width = c.height = N;
  const ctx = c.getContext("2d");

  ctx.fillStyle = "#8d949d";
  ctx.fillRect(0, 0, N, N);

  // 云斑（树脂流动造成的深浅不均）
  for (let i = 0; i < 260; i++) {
    const x = Math.random() * N, y = Math.random() * N;
    const r = 6 + Math.random() * 34;
    const light = Math.random() > 0.5;
    const g = ctx.createRadialGradient(x, y, 0, x, y, r);
    g.addColorStop(0, light ? "rgba(255,255,255,0.055)" : "rgba(0,0,0,0.05)");
    g.addColorStop(1, "rgba(0,0,0,0)");
    ctx.fillStyle = g;
    ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.fill();
  }
  // 骨料斑点（自流平的颗粒感）
  for (let i = 0; i < 2600; i++) {
    const v = Math.random();
    ctx.fillStyle = v > 0.5 ? "rgba(255,255,255,0.07)" : "rgba(0,0,0,0.06)";
    ctx.fillRect(Math.random() * N, Math.random() * N, 1.4, 1.4);
  }
  // 板缝：四周各一道深色细线（平铺后形成整齐的板块网格）
  ctx.strokeStyle = "rgba(70,77,86,0.85)";
  ctx.lineWidth = 3;
  ctx.strokeRect(1.5, 1.5, N - 3, N - 3);

  const tex = new THREE.CanvasTexture(c);
  tex.wrapS = tex.wrapT = THREE.RepeatWrapping;
  tex.anisotropy = 4;
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

/**
 * 黄黑 45° 警示斜纹（工业设备基座的标志性元素，比纯色黄条醒目得多）。
 * ★ 斜纹要**等比例**：贴图单元在 UV 空间是正方形，所以调用方必须按
 *   `repeat = (面宽/单元边长, 面高/单元边长)` 设置，否则斜纹会被拉成横条。
 */
export const HAZARD_TILE = 0.28;     // 一个斜纹单元在现实中的边长(m)
function makeHazardTexture() {
  const N = 128;
  const c = document.createElement("canvas");
  c.width = c.height = N;
  const ctx = c.getContext("2d");
  ctx.fillStyle = "#1abdd6";  // 青色主题
  ctx.fillRect(0, 0, N, N);
  ctx.strokeStyle = "#0a2a30";
  ctx.lineWidth = N / 5;
  // 45° 斜线：从左下往右上铺满整张图（含两侧溢出，保证平铺无缝）
  for (let x = -2 * N; x <= 2 * N; x += N / 2.5) {
    ctx.beginPath();
    ctx.moveTo(x, N);
    ctx.lineTo(x + N, 0);
    ctx.stroke();
  }
  const tex = new THREE.CanvasTexture(c);
  tex.wrapS = tex.wrapT = THREE.RepeatWrapping;
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.anisotropy = 4;
  return tex;
}

/** 径向渐变"接触阴影"：中心半透明黑 → 边缘透明（机器人落地的暗影）。 */
function makeShadowTexture() {
  const N = 128;
  const c = document.createElement("canvas");
  c.width = c.height = N;
  const ctx = c.getContext("2d");
  const g = ctx.createRadialGradient(N / 2, N / 2, 6, N / 2, N / 2, N / 2);
  g.addColorStop(0, "rgba(0,0,0,0.46)");
  g.addColorStop(0.52, "rgba(0,0,0,0.22)");
  g.addColorStop(1, "rgba(0,0,0,0)");
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, N, N);
  const tex = new THREE.CanvasTexture(c);
  tex.anisotropy = 4;
  return tex;
}

/** 指定间距的水平正交网格线段（XZ 平面），比 GridHelper 更可控。 */
function makeGrid(size, step, color, opacity) {
  const pts = [];
  const h = size / 2;
  for (let v = -h; v <= h + 1e-6; v += step) {
    pts.push(-h, 0, v, h, 0, v);
    pts.push(v, 0, -h, v, 0, h);
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.Float32BufferAttribute(pts, 3));
  const mat = new THREE.LineBasicMaterial({
    color, transparent: true, opacity, depthWrite: false,
  });
  return new THREE.LineSegments(geo, mat);
}

/**
 * @param {{size?:number, step?:number}} opt
 * @returns {{group: THREE.Group, dispose: () => void}}
 */
export function buildCell(opt = {}) {
  const size = opt.size || FLOOR_SIZE;
  const step = opt.step || GRID_STEP;
  const group = new THREE.Group();
  group.name = "cell";
  const disposables = [];

  // ---- 深色科技地坪（深蓝黑 + 青色网格线） ----
  const floorGeo = new THREE.PlaneGeometry(size, size);
  const floorMat = new THREE.MeshPhysicalMaterial({
    color: 0x0a1628,
    metalness: 0.3,
    roughness: 0.55,
    clearcoat: 0.3,
    clearcoatRoughness: 0.4,
    envMapIntensity: 0.8,
  });
  const floor = new THREE.Mesh(floorGeo, floorMat);
  floor.rotation.x = -Math.PI / 2;
  floor.position.y = -0.002;
  floor.name = "floor";
  group.add(floor);
  disposables.push(floorGeo, floorMat);

  // ---- 青色工程细网格（淡淡压在地坪上） ----
  const fine = makeGrid(size, step, 0x2fd6f0, 0.15);
  fine.position.y = 0.0004;
  fine.name = "floor-grid";
  group.add(fine);
  disposables.push(fine.geometry, fine.material);

  // ---- 工作区青色地标线：3.6m 场地四边（主题色分区线） ----
  const bandMat = new THREE.MeshStandardMaterial({
    color: 0x2fd6f0, metalness: 0.1, roughness: 0.5,
    emissive: 0x2fd6f0, emissiveIntensity: 0.15,
  });
  const bw = 0.07;
  const half = size / 2 - bw / 2;
  for (const [w, d, x, z] of [
    [size, bw, 0, -half], [size, bw, 0, half],
    [bw, size, -half, 0], [bw, size, half, 0],
  ]) {
    const g = new THREE.PlaneGeometry(w, d);
    const m = new THREE.Mesh(g, bandMat);
    m.rotation.x = -Math.PI / 2;
    m.position.set(x, 0.0018, z);
    m.name = "floor-band";
    group.add(m);
    disposables.push(g);
  }
  disposables.push(bandMat);

  // ---- 机器人站台（设备基座） ----
  // ★ 台面**顶面正好在 y = PLATFORM_H**（机器人立足面），机器人抬到 PLATFORM_H 就"站"在台面上。
  //   自下而上七层（截面是"下窄上宽"的设备基座，不是一块光板）：
  //     调平脚 0~0.020 → 踢脚底盘 0.020~0.065 → 台身 0.065~0.150（嵌黄黑警示斜纹带 0.072~0.102）
  //     → 台面檐口 0.134~0.150 → 拉丝台面 0.150~0.180；铭牌嵌在台身上 0.110~0.134
  const plat = new THREE.Group();
  plat.name = "platform";
  const bodyMat = new THREE.MeshStandardMaterial({ color: 0x454c55, metalness: 0.55, roughness: 0.42 });
  const plinthMat = new THREE.MeshStandardMaterial({ color: 0x2b3037, metalness: 0.5, roughness: 0.5 });
  const rimMat = new THREE.MeshStandardMaterial({ color: 0x33393f, metalness: 0.6, roughness: 0.35 });
  const footMat = new THREE.MeshStandardMaterial({ color: 0xb9bfc7, metalness: 0.85, roughness: 0.3 });
  const topMat = new THREE.MeshStandardMaterial({ color: 0x9aa2ac, metalness: 0.85, roughness: 0.24 });
  const plateMat = new THREE.MeshStandardMaterial({ color: 0x1b1f24, metalness: 0.4, roughness: 0.5 });
  const hazTex = makeHazardTexture();
  // 斜纹等比例：单元边长 HAZARD_TILE，按"面宽/面高"分别设 repeat
  const bandW = PLATFORM_SIZE + 0.006, bandH = 0.030;
  hazTex.repeat.set(bandW / HAZARD_TILE, bandH / HAZARD_TILE);
  const hazMat = new THREE.MeshStandardMaterial({
    map: hazTex, metalness: 0.15, roughness: 0.62,
  });

  const mkBox = (w, h, d, mat, x, y, z, name) => {
    const g = new THREE.BoxGeometry(w, h, d);
    const m = new THREE.Mesh(g, mat);
    m.position.set(x, y, z);
    m.name = name;
    plat.add(m);
    disposables.push(g);
    return m;
  };
  const mkCyl = (r, h, mat, x, y, z, name) => {
    const g = new THREE.CylinderGeometry(r, r, h, 20);
    const m = new THREE.Mesh(g, mat);
    m.position.set(x, y, z);
    m.name = name;
    plat.add(m);
    disposables.push(g);
    return m;
  };

  // ① 可调调平脚：4 个，落在四角、从踢脚底盘下方探出（真正贴地的是它）
  const footAt = 0.50;
  for (const [fx, fz] of [[-1, -1], [1, -1], [-1, 1], [1, 1]]) {
    mkCyl(0.065, 0.020, footMat, fx * footAt, 0.010, fz * footAt, "platform-foot");
    mkCyl(0.026, 0.026, plinthMat, fx * footAt, 0.033, fz * footAt, "platform-footbolt");
  }
  // ② 踢脚底盘（内收 → 台身看起来"浮"在地面上）
  const plinthW = PLATFORM_SIZE - 0.20;
  mkBox(plinthW, 0.045, plinthW, plinthMat, 0, 0.0425, 0, "platform-plinth");
  // ③ 台身（外挑于踢脚）
  mkBox(PLATFORM_SIZE, 0.085, PLATFORM_SIZE, bodyMat, 0, 0.1075, 0, "platform-body");
  // ④ 黄黑警示斜纹带（嵌在台身下部，略微外凸 3mm）
  mkBox(bandW, bandH, bandW, hazMat, 0, 0.087, 0, "platform-band");
  // ⑤ 台面檐口（比台身外挑 4.5cm，形成台面投下的阴影线）
  mkBox(PLATFORM_SIZE + 0.09, 0.016, PLATFORM_SIZE + 0.09, rimMat, 0, 0.142, 0, "platform-toprim");
  // ⑥ 拉丝不锈钢台面：**顶面正好 y = PLATFORM_H**
  mkBox(PLATFORM_TOP_SIZE, 0.030, PLATFORM_TOP_SIZE, topMat, 0, PLATFORM_H - 0.015, 0, "platform-top");
  // ⑦ 正面铭牌：嵌在台身上、**警示带与台面檐口之间**（上下都不重叠，否则会被埋进檐口）
  mkBox(0.30, 0.024, 0.012, plateMat, 0, 0.122, PLATFORM_SIZE / 2 + 0.006, "platform-plate");
  group.add(plat);
  disposables.push(bodyMat, plinthMat, rimMat, footMat, topMat, plateMat, hazMat, hazTex);

  // ---- 机器人接触阴影（落在**台面**上；必须小于台面，否则会糊出台沿） ----
  const shTex = makeShadowTexture();
  const shGeo = new THREE.PlaneGeometry(PLATFORM_SIZE * 0.92, PLATFORM_SIZE * 0.92);
  const shMat = new THREE.MeshBasicMaterial({
    map: shTex, transparent: true, depthWrite: false,
  });
  const shadow = new THREE.Mesh(shGeo, shMat);
  shadow.rotation.x = -Math.PI / 2;
  shadow.position.y = PLATFORM_H + 0.0015;
  shadow.renderOrder = 0.5;
  shadow.name = "floor-shadow";
  group.add(shadow);
  disposables.push(shGeo, shMat, shTex);

  function dispose() { disposables.forEach((d) => d.dispose()); }

  return { group, dispose };
}
