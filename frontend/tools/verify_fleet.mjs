// =====================================================================
// 回归测试：src/three/fleet.js —— 机房 12 台的摆放/围栏/状态隔离。
//
// 这里钉的是**前端把机房地图翻译成 3D** 这一段，它一旦错就是"整屏都是错的"：
//   · 地面坐标 → Three 的映射（y→−z、yaw→rotation.y 的符号）
//   · 12 块围栏/报警垫是否各自跟着自己的工位（而不是 12 份全叠在机房原点）
//   · 一台报警会不会传染给隔壁 11 台（SafetyFence 实例必须真独立）
//   · 几何共享（12 次 clone 不能变成 12 份显存）
//
// 工位坐标用的是 /api/fleet 的真实输出形状；这套布局本身由
// backend/tests/test_fleet.py 钉住（12 台、两个区、间距 1.64）。
// 本脚本额外核对一条跨文件不变量：fleet.yaml 的 cell_pitch_m 必须等于
// safety.json 的围栏边长 —— 两者一个是"阵列间距"、一个是"围栏尺寸"，
// 对不上就说明机房地图和报警边界各说各话。
//
// 运行： cd frontend && node tools/verify_fleet.mjs
// =====================================================================
import fs from "node:fs";
import path from "node:path";
import * as THREE from "three";
import { buildFleet } from "../src/three/fleet.js";
import { buildRobot } from "../src/robot/robotModel.js";
import { FALLBACK_DH } from "../src/config.js";
import { defaultSafety } from "../src/utils/safetyConfig.js";

const rootDir = path.resolve(process.cwd(), "..");
const read = (rel) => fs.readFileSync(path.join(rootDir, rel), "utf8");

let fail = 0;
function check(ok, msg) {
  if (!ok) fail++;
  console.log(`  ${ok ? "PASS" : "FAIL"}  ${msg}`);
}

// ---------------------------------------------------------------------
// 工位表：与 /api/fleet 的 robots[] 同构（pos 由后端 derive_positions 算出）
// ---------------------------------------------------------------------
const PITCH = 1.64, ZONE_GAP = 1.00, GROUP_GAP = 1.00;
const zoneX = { A: -(PITCH / 2 + ZONE_GAP / 2), B: (PITCH / 2 + ZONE_GAP / 2) };
const lower = [0, PITCH, PITCH * 2];
const upperStart = lower[2] + PITCH / 2 + GROUP_GAP + PITCH / 2;
const upper = [upperStart, upperStart + PITCH, upperStart + PITCH * 2];
const mid = (upper[2] + lower[0]) / 2;
const rows = { lower, upper };

function mkRobot(id, zone, group, slot, model) {
  const y = rows[group][slot - 1] - mid;
  return {
    id, name: `${zone}${group}-${slot}`, zone, group, slot, order: Number(id),
    model, brand: model === "huayan" ? "HUAYAN" : "EFORT",
    host: "192.168.1.10", port: model === "huayan" ? 10004 : 502,
    pos: { x: zoneX[zone], y, yaw: 0 },
  };
}
const ROBOTS = [];
["A", "B"].forEach((zone) => {
  ["lower", "upper"].forEach((group) => {
    [1, 2, 3].forEach((slot) => {
      ROBOTS.push(mkRobot(`${zone}${group}${slot}`, zone, group, slot,
        zone === "A" ? "efort" : "huayan"));
    });
  });
});

// ---------------------------------------------------------------------
console.log("=== 0) 跨文件不变量：阵列间距 == 围栏边长 ===");
{
  const yaml = read("config/fleet.yaml");
  const pitch = Number(yaml.match(/^\s*cell_pitch_m:\s*([0-9.]+)/m)[1]);
  const half = JSON.parse(read("config/safety.json")).zones[0].half;
  check(Math.abs(pitch - half.x * 2) < 1e-9 && Math.abs(pitch - half.z * 2) < 1e-9,
    `fleet.yaml cell_pitch_m=${pitch} == safety.json half(${half.x},${half.z})×2 —— 围栏正好铺满阵列`);
}

// ---------------------------------------------------------------------
console.log("\n=== 1) 12 台全部建起来 ===");
const bodies = {
  efort: (() => {
    const m = buildRobot(FALLBACK_DH, "floor");
    return { root: m.root, offsets: FALLBACK_DH.joints.map((j) => j.theta_offset || 0) };
  })(),
  huayan: (() => {
    const m = buildRobot(FALLBACK_DH, "floor");
    return { root: m.root, offsets: [0, 0, 0, 0, 0, 0] };
  })(),
};
const scene = new THREE.Scene();
const cfg = defaultSafety();
const fleet = buildFleet({
  scene, robots: ROBOTS, layout: { cell_pitch_m: PITCH }, safetyCfg: cfg, bodies,
});
check(fleet.stations.length === 12, `建出 12 个工位（实际 ${fleet.stations.length}）`);
check(fleet.unbuilt.length === 0, `没有建不起来的工位（实际 ${JSON.stringify(fleet.unbuilt)}）`);
check(!!scene.getObjectByName("fleet"), "fleet 根组已加入场景");

// ---------------------------------------------------------------------
console.log("\n=== 2) 地面坐标 → Three：x→x、y→−z、yaw→+rotation.y ===");
{
  const bad = ROBOTS.filter((r) => {
    const s = fleet.stations.find((x) => x.id === r.id);
    return !(Math.abs(s.group.position.x - r.pos.x) < 1e-9
      && Math.abs(s.group.position.y) < 1e-9
      && Math.abs(s.group.position.z + r.pos.y) < 1e-9);
  });
  check(bad.length === 0, bad.length
    ? `落点不符的工位: ${bad.map((r) => {
        const s = fleet.stations.find((x) => x.id === r.id);
        return `${r.id} → ${s.group.position.toArray()} 应为 [${r.pos.x}, 0, ${-r.pos.y}]`;
      }).join(" | ")}`
    : "12 个工位组的落点与登记表一致（y 取反这条最容易漏）");

  // 偏航符号：机器人朝"纵深 +y"时，本体 +X 必须落到 Three 的 −z。
  const scene2 = new THREE.Scene();
  const yawed = buildFleet({
    scene: scene2,
    robots: [Object.assign({}, ROBOTS[0], { id: "Y1", pos: { x: 0, y: 0, yaw: 90 } })],
    layout: { cell_pitch_m: PITCH }, safetyCfg: cfg, bodies,
  });
  scene2.updateMatrixWorld(true);
  const g = yawed.stations[0].group;
  const fwd = g.localToWorld(new THREE.Vector3(1, 0, 0))
    .sub(g.getWorldPosition(new THREE.Vector3()));
  check(Math.abs(fwd.z + 1) < 1e-6 && Math.abs(fwd.x) < 1e-6,
    `yaw=90° 时本体 +X 指向 (${fwd.x.toFixed(2)}, ${fwd.z.toFixed(2)}) —— 从 +x 转向 +y 为正，与 SimMonitor 的 dh.y=−three.z 同一套`);
  yawed.dispose();
}

// ---------------------------------------------------------------------
console.log("\n=== 3) 围栏与脚下刻度盘跟着自己的工位走（不是 12 份叠在原点） ===");
{
  scene.updateMatrixWorld(true);
  // ★ 量的是**世界包围盒中心**，不是 object.position：SafetyFence 的区域顶点已经
  //   带上了 center 偏移（几何里），mesh 自身的 position 只有 y。
  const centerOf = (obj) => new THREE.Box3().setFromObject(obj).getCenter(new THREE.Vector3());
  // 机房大屏把地面报警垫**关掉了**（12 张发光地毯会把等轴画面糊成一片，状态改由
  // 围栏墙色 + 脚下 HUD 刻度盘 + 卡片色条表达）。这里量的是围栏区域本体：
  // "每套围栏跟着自己的工位"这条不变量与垫子在不在无关，必须照样钉住。
  const zones = fleet.stations.map((s) => {
    const g = s.fence && s.fence.group.getObjectByName("ground-zone");
    return g ? centerOf(g) : centerOf(s.fence.zones[0].group);
  });
  check(zones.every((p) => p && Number.isFinite(p.x)), "每个工位都有一套自己的围栏区域");
  const off = zones.filter((p) => Math.hypot(p.x, p.z) > 0.5).length;
  check(off === 12, `${off}/12 套围栏离开了原点（少于 12 = 有机位的围栏叠回机房中间，正是 setPadPlane 那个坑）`);
  const s0 = fleet.stations[0];
  const pad = zones[0];
  check(Math.abs(pad.x - s0.group.position.x) < 1e-6 && Math.abs(pad.z - s0.group.position.z) < 1e-6,
    `围栏区域中心 = 工位中心 (${pad.x.toFixed(2)}, ${pad.z.toFixed(2)}) vs 工位 (${s0.group.position.x.toFixed(2)}, ${s0.group.position.z.toFixed(2)})`);
  const fenceBox = new THREE.Box3().setFromObject(s0.fence.zones[0].group);
  check(Math.abs((fenceBox.min.x + fenceBox.max.x) / 2 - s0.group.position.x) < 0.05
    && Math.abs((fenceBox.min.z + fenceBox.max.z) / 2 - s0.group.position.z) < 0.05,
    `围栏墙体的水平中心也落在本工位（宽 ${(fenceBox.max.x - fenceBox.min.x).toFixed(2)}m）`);
  // 相邻两台：区域中心距 == 间距 → 正好贴着，不重叠也不留缝
  const ids = fleet.stations.map((s) => s.id);
  const a = zones[ids.indexOf("Alower1")], b = zones[ids.indexOf("Alower2")];
  const d = Math.hypot(a.x - b.x, a.z - b.z);
  check(Math.abs(d - PITCH) < 1e-6, `同组相邻两台的围栏中心距 ${d.toFixed(3)}m == 间距 ${PITCH}m`);
  check(!!s0.fence.zones[0] && s0.fence.zones[0].walls.length === 4, "每台围栏 4 面墙（逐面墙独立变色）");
  const matA = s0.fence.zones[0].walls[0].panelMat;
  const matB = fleet.stations[1].fence.zones[0].walls[0].panelMat;
  check(matA !== matB, "相邻两台的墙面材质不是同一个对象（否则一台红、四面全红）");
  check(s0.fence.padPlane === null, "机房层不设 padPlane（那是以原点为中心的方形，12 台会全叠中间）");
  // 地面垫关掉 ≠ 状态没人显示：每台必须有自己的那圈刻度盘，且跟着工位走
  check(fleet.stations.every((s) => !!s.hud), "每个工位都有一圈自己的 HUD 刻度盘");
  const ring = centerOf(fleet.stations[0].hud.group);
  check(Math.abs(ring.x - s0.group.position.x) < 0.05 && Math.abs(ring.z - s0.group.position.z) < 0.05,
    `刻度盘落在自己工位脚下 (${ring.x.toFixed(2)}, ${ring.z.toFixed(2)})`);
  // 报警垫**必须照样建**：地面碰撞判据（机械臂低过立足面）是跟着这块垫子走的。
  // 试过 ground.enabled:false —— 画面干净了，但第 4 节"折臂报警"当场测不出来，
  // 等于为了好看关掉一半安全检查。机房只把**安全档**调成全透明：
  // 健康的机位脚下是空的，逼近/碰撞时这块垫子自己会亮，比 12 张常亮地毯更醒目。
  check(fleet.stations.every((s) => !!s.fence.groundMesh),
    "每个工位仍有地面报警垫（地面碰撞判据依赖它，不能为了画面干净把它关掉）");
  check(fleet.stations.every((s) => s.fence.groundMat.opacity === 0),
    "安全档的垫子全透明（等轴视角下 12 张发光地毯会糊成一片），报警档才亮");
}

// ---------------------------------------------------------------------
console.log("\n=== 4) 一台折臂报警，隔壁 11 台不受传染 ===");
{
  const r0 = fleet.update(0);
  check(r0.list.every((x) => x.state === "safe"), `零位时 12 台都安全（worst=${r0.worst && r0.worst.state}）`);
  // 深折到小臂/腕低过立足面：只有这一台的地面报警该亮。
  const victim = fleet.stations[0].id;
  fleet.setPose(victim, [0, -210, -60, 0, -80, 0]);
  for (let i = 0; i < 400; i++) fleet.step(1 / 60);        // 让平滑收敛到位
  const r1 = fleet.update(0);
  const mine = r1.list.find((x) => x.id === victim);
  const others = r1.list.filter((x) => x.id !== victim);
  check(mine.state !== "safe", `${victim} 折臂后状态=${mine.state} 净空=${(mine.clearance * 1000).toFixed(0)}mm`);
  check(others.every((x) => x.state === "safe"),
    `其余 ${others.length} 台仍为 safe（传染说明围栏实例共用了状态）`);
  check(r1.worst.id === victim, `总最严重取到的是这台：${r1.worst && r1.worst.id}`);
  // 复位，后面几节从干净状态开始
  fleet.setPose(victim, [0, 0, 0, 0, 0, 0]);
  for (let i = 0; i < 400; i++) fleet.step(1 / 60);
  const r2 = fleet.update(0);
  check(r2.list.every((x) => x.state === "safe"), "复位后回到全 safe");
}

// ---------------------------------------------------------------------
console.log("\n=== 5) 12 次 clone 不产生 12 份几何（显存预算） ===");
{
  const geo = new Set();
  fleet.stations.forEach((s) => s.root.traverse((o) => { if (o.isMesh) geo.add(o.geometry.uuid); }));
  let tpl = 0;
  bodies.efort.root.traverse((o) => { if (o.isMesh) tpl++; });
  check(geo.size === tpl || geo.size <= tpl * 2,
    `12 台只用了 ${geo.size} 种几何（一台模板 ${tpl} 个网格；若等于 12×${tpl} 就是没共享）`);
  // 判据不是拍一个"< 20"的数字，而是"材质种类 == 模板材质种类的并集"：
  // clone 共享材质，所以 12 台的材质集合 = 两个模板（efort + huayan）的材质集合。
  // 若材质随台数线性涨（= 12×每台一份），说明 clone 把材质也复制了。
  const matsOf = (o) => {
    const s = new Set();
    o.traverse((n) => {
      (Array.isArray(n.material) ? n.material : [n.material]).forEach((m) => m && s.add(m.uuid));
    });
    return s;
  };
  const tplMats = new Set([...matsOf(bodies.efort.root), ...matsOf(bodies.huayan.root)]);
  const perStation = matsOf(fleet.stations[0].root).size;
  // 只统计机械本体（fleet.group 里还挂着 12 套围栏，那是**故意**独立的，见第 3 节）
  const mats = new Set();
  fleet.stations.forEach((s) => matsOf(s.root).forEach((u) => mats.add(u)));
  check(mats.size === tplMats.size,
    `12 台的材质种类 = 模板并集 ${tplMats.size} 个（实际 ${mats.size}）；单台 ${perStation} 个 → 若每台各造一份应是 ${perStation * fleet.stations.length} 个`);
}

// ---------------------------------------------------------------------
console.log("\n=== 6) 跨过 ±180° 走最短路径（真机 J1 过零位时画面不能倒转一圈） ===");
{
  const s = fleet.stations[3];
  fleet.setPose(s.id, [179, 0, 0, 0, 0, 0]);
  for (let i = 0; i < 400; i++) fleet.step(1 / 60);
  const before = s.joints[0].rotation.z;
  fleet.setPose(s.id, [-179, 0, 0, 0, 0, 0]);   // 实际只差 2°
  fleet.step(1 / 60);
  const after = s.joints[0].rotation.z;
  const d = Math.abs(after - before);
  check(d < 0.02, `179°→−179° 单帧只转了 ${(d * 180 / Math.PI).toFixed(2)}°（倒回 358° 就是画面抽风）`);
  // 主值归一：平滑过程中累计值不该越跑越远（armChain 那边有 ±360 的兜底夹取）
  check(Math.abs(after) <= Math.PI + 1e-6, `写进 rotation.z 的角度仍在主值区间：${(after * 180 / Math.PI).toFixed(1)}°`);
}

// ---------------------------------------------------------------------
console.log("\n=== 7) 坏帧/缺项：整帧丢弃，别的机器不受影响 ===");
{
  const s = fleet.stations[5];
  fleet.setPose(s.id, [30, 0, 0, 0, 0, 0]);
  for (let i = 0; i < 400; i++) fleet.step(1 / 60);
  const snap = s.joints[0].rotation.z;
  fleet.setPose(s.id, [30, NaN, 0, 0, 0, 0]);
  for (let i = 0; i < 40; i++) fleet.step(1 / 60);
  check(Math.abs(s.joints[0].rotation.z - snap) < 1e-9, "含 NaN 的一帧被整帧丢弃（不会把其余五轴甩回零位）");
  fleet.setPose(s.id, null);
  fleet.setPoses({ "不存在的ID": [1, 2, 3, 4, 5, 6] });
  check(Math.abs(s.joints[0].rotation.z - snap) < 1e-9, "未知 robot_id / 空帧只是被忽略，不抛异常");
}

// ---------------------------------------------------------------------
console.log("\n=== 8) 建不起来的那台要留下原因，不能整层消失 ===");
{
  const partial = buildFleet({
    scene: new THREE.Scene(),
    robots: [ROBOTS[0], Object.assign({}, ROBOTS[1], { model: "unknown_model" })],
    layout: { cell_pitch_m: PITCH }, safetyCfg: cfg, bodies,
  });
  check(partial.stations.length === 1, "认得出的那台照常建起来");
  check(partial.unbuilt.length === 1 && partial.unbuilt[0].reason.includes("模型档案"),
    `认不出的型号进了 unbuilt: ${JSON.stringify(partial.unbuilt)}`);
  partial.dispose();
}

// ---------------------------------------------------------------------
console.log("\n=== 9) 显隐与释放 ===");
{
  fleet.setVisible(false);
  check(fleet.group.visible === false, "隐藏 = 只切根组可见性（不重建、不回收资源）");
  fleet.setVisible(true);
  const before = scene.children.length;
  fleet.dispose();
  check(scene.children.length === before - 1 && fleet.stations.length === 0,
    `dispose 从场景摘掉 fleet 组并清空工位表（${before}→${scene.children.length}）`);
  // clone 的几何与模板共享：dispose 不能把它释放掉，否则单机层/下次重建拿到已释放几何
  let alive = 0;
  bodies.efort.root.traverse((o) => { if (o.isMesh && o.geometry && o.geometry.attributes.position) alive++; });
  check(alive > 0, `模板几何在 fleet.dispose() 之后仍可用（${alive} 个网格）`);
}

console.log(`\n===== 结果: ${fail === 0 ? "全部通过" : fail + " 项失败"} =====`);
process.exit(fail === 0 ? 0 : 1);
