// =====================================================================
// 分拣 3D 可视化回归（headless，node 直接跑）
//
//   钉死 4 类最容易静默出错、而界面上"看着还挺正常"的东西：
//   A. 坐标映射：`sceneCoords.sceneFromBaseMm()` 必须与 three 的 DH 链**逐点对上**
//      —— 这是方案里"坐标系对齐没实证"那条的落地校验。做法与 verify_kinematics 一致：
//      同一套 DH + 同一个根变换（rotX(-90°) + scale 0.001），用后端 FK 的参考值反查。
//   B. 摆位规则：方块底面统一贴**摆放面** plane_h_mm（默认 0 = 站台台面，**不漂浮**）；
//      未示教/重叠的槽位**不画**；A/B 各一个**托盘盒**（盒底与料同平面）；
//      摄像头**坐在台面上、镜头朝上**；★ 反证：plane_z 变化不影响摆放高度。
//   C. 状态映射：A 格随"已放走件数"消失；失败停止 → 方块**画回并标红**；
//      人工停止（holding）→ 留在手上；B 格按 placement 出现同色实心块。
//   D. 资源：dispose 后根节点无子节点、几何/材质全部释放（反复进出页面不泄漏）。
//
// 运行： cd frontend && node tools/verify_sorting3d.mjs
// =====================================================================
import * as THREE from "three";
import { sceneFromBaseMm } from "../src/three/sceneCoords.js";
import { createSortingBlocks, SORT_COLOR_HEX, HEX_SUSPECT } from "../src/three/sortingBlocks.js";

let pass = 0, fail = 0;
function ok(name, cond, extra = "") {
  if (cond) { pass++; console.log("  PASS  " + name); }
  else { fail++; console.log("  FAIL  " + name + (extra ? "   → " + extra : "")); }
}

const deg2rad = (d) => (d * Math.PI) / 180;
const LIFT = 0.18;                    // 与 cell.PLATFORM_H 一致

// ---- 与 config/robot.yaml 一致（同 verify_kinematics.mjs） ----
const DH = [
  { d: 376.0, a: 49.5932, alpha: 90.0, theta_offset: 0.0 },
  { d: 0.2852, a: 330.1834, alpha: 0.0, theta_offset: 90.0 },
  { d: 0.0, a: 40.2714, alpha: 90.0, theta_offset: 0.0 },
  { d: 329.2414, a: 0.0, alpha: -90.0, theta_offset: 0.0 },
  { d: 0.0, a: 0.0, alpha: 90.0, theta_offset: 0.0 },
  { d: 80.0, a: 0.0, alpha: 0.0, theta_offset: 0.0 },
];
// 后端 kinematics.forward_kinematics 给出的法兰位置（基座系 mm）
const REF = {
  "0,0,0,0,0,0": [458.8346, -0.2852, 746.4548],
  "0,30,0,0,0,0": [218.7792, -0.2852, 901.444],
  "45,-20,35,10,-40,25": [377.4398, 389.6647, 777.3292],
  "-60,50,-30,90,60,-120": [4.6819, -147.2438, 752.3685],
};

// ---- 复刻 manager 里模型根节点的变换链（与官方 GLB / 程序化模型同一条） ----
function buildChain() {
  const root = new THREE.Group();
  root.rotation.x = -Math.PI / 2;
  root.scale.setScalar(0.001);
  root.position.y = LIFT;                       // manager: robot.root.position.y = baseLiftY()
  const joints = [];
  let parent = root;
  for (let i = 0; i < DH.length; i++) {
    const j = DH[i];
    const jg = new THREE.Group(); parent.add(jg);
    const dz = new THREE.Group(); dz.position.z = j.d; jg.add(dz);
    const ax = new THREE.Group(); ax.position.x = j.a; dz.add(ax);
    const rx = new THREE.Group(); rx.rotation.x = deg2rad(j.alpha); ax.add(rx);
    joints.push(jg);
    parent = rx;
  }
  const tcp = new THREE.Group(); parent.add(tcp);
  return { root, joints, tcp };
}

const { readFileSync } = await import("node:fs");
const { dirname: _dn, join: _jn } = await import("node:path");
const { fileURLToPath: _fu } = await import("node:url");
const _root = _jn(_dn(_fu(import.meta.url)), "..");
const read = (rel) => readFileSync(_jn(_root, rel), "utf8");

console.log("== A. 坐标映射：sceneCoords.sceneFromBaseMm vs three DH 链 ==");
{
  const c = buildChain();
  const got = [];
  for (const [k, ref] of Object.entries(REF)) {
    const q = k.split(",").map(Number);
    for (let i = 0; i < c.joints.length; i++) {
      c.joints[i].rotation.z = deg2rad((q[i] || 0) + DH[i].theta_offset);
    }
    c.root.updateMatrixWorld(true);
    const v = new THREE.Vector3();
    c.tcp.getWorldPosition(v);
    const mine = sceneFromBaseMm(ref[0], ref[1], ref[2], LIFT);
    const d = Math.hypot(v.x - mine.x, v.y - mine.y, v.z - mine.z) * 1000;  // mm
    got.push(d);
    ok(`「${k}」映射吻合（差 ${d.toFixed(3)} mm）`, d < 0.5);
  }
  ok("映射不是恒等式（确实做了 rotX + 单位换算）",
    (() => {
      const m = sceneFromBaseMm(0, 1000, 0, 0);
      return Math.abs(m.z + 1) < 1e-9 && Math.abs(m.y) < 1e-9;   // 基座 +Y → 场景 −Z
    })());
}

console.log("");
console.log("== B. 摆位规则：共面 / 未示教不画 / 重叠不画 / 托盘盒 / 摄像头落地朝上 ==");
const SLOTS_OK = [
  { name: "A1", role: "A", pos: [335.56, 397.55, 145.94], taught: true, row: 1, col: 1 },
  { name: "A2", role: "A", pos: [331.65, 312.15, 144.73], taught: true, row: 1, col: 2 },
  { name: "A3", role: "A", pos: null, taught: false, row: 1, col: 3 },
  { name: "B1", role: "B", pos: [433.40, 397.63, 148.85], taught: true, row: 1, col: 1 },
  { name: "B4", role: "B", pos: [433.80, 312.00, 149.00], taught: true, row: 2, col: 1 },
];
const PAYLOAD = {
  slots: SLOTS_OK, plane_z: 147.0, plane_h_mm: 0, block_mm: [70, 70, 14],
  camera: { name: "C检测", pos: [478.81, -7.44, 294.48], taught: true },
  camera_height_mm: 0, overlaps: [], missing: ["A3"], plane_spread: 2.1, plane_tol: 5,
};
{
  const sb = createSortingBlocks(LIFT);
  sb.setSlots(PAYLOAD);
  const info = sb.info();
  ok("已示教的 A 槽画出来（A1/A2）", info.a.includes("A1") && info.a.includes("A2"));
  ok("未示教的 A3 不画，且记了原因",
    !info.a.includes("A3") && info.warned.some((w) => w.includes("A3")));
  ok("B 槽底框画出来（B1/B4）", info.b.includes("B1") && info.b.includes("B4"));
  ok("C 区摄像头模型已建", info.camera === true);

  // ★★ 用户 2026-10-08 第三轮："19 个摆放的 3D 物体怎么悬浮起来了啊，放在地上啊"
  //    根因：把示教点的 Z（FK 给的是**法兰中心**）当成了料的高度 → 料被抬到台面上方 15cm。
  //    下面三条把正确口径钉死：**摆放面 plane_h_mm（默认 0 = 站台台面）**，与 plane_z 无关。
  ok("★ 摆放面取 plane_h_mm，不取 plane_z",
    info.planeH === 0 && info.planeZ === 147.0);
  ok("★ 摄像头底座坐在摆放面上（Z = plane_h + cam_h = 0，不是 plane_z=147）",
    Array.isArray(info.camBase) && Math.abs(info.camBase[2] - 0) < 1e-9,
    "实际: " + JSON.stringify(info.camBase));
  ok("★ 摄像头不再用示教点 Z（示教 Z=294 若被采用就会飘在半空）",
    Array.isArray(info.camBase) && Math.abs(info.camBase[2] - 294.48) > 1);
  // ★★ 反证：把 plane_z 改到离谱的值，**摆放高度必须纹丝不动**
  //    （若哪天又退回"用 plane_z 当高度"，这条立刻红）
  {
    const sbZ = createSortingBlocks(LIFT);
    sbZ.setSlots({ ...PAYLOAD, plane_z: 400.0 });
    const iZ = sbZ.info();
    const ys = [], traysY = [];
    sbZ.root.children.forEach((c) => {
      const u = c.userData || {};
      if (u.sortName || u.sortSlot || u.sortSocket) ys.push(c.position.y);
      if (u.sortTray) traysY.push(c.children[0].position.y);
    });
    const piece = LIFT + 4 / 1000 + 0.014 / 2;    // 料中心 = 台面 + 托盘板厚 + 半厚
    const tray = LIFT + 4 / 1000 / 2;             // 托盘底板中心 = 台面 + 半板厚
    ok("★★ 反证：plane_z=400 时摆放高度完全不变（不再把法兰高当料高）",
      ys.length >= 6 && ys.every((y) => Math.abs(y - piece) < 1e-9)
      && traysY.length === 2 && traysY.every((y) => Math.abs(y - tray) < 1e-9)
      && Math.abs(iZ.camBase[2] - 0) < 1e-9,
      `料Y=${JSON.stringify(ys.slice(0, 3))} 托盘Y=${JSON.stringify(traysY)}`);
    sbZ.dispose();
  }
  // ★★ 落地检查（与 tools/preview_sorting3d.mjs 同口径，进回归链才能防回退）：
  //    托盘底面/相机底面 → 台面；料底面 → 托盘内底面。一个都不许悬空。
  ok("★★ 全部落到实处：21 个物体底面偏差 < 0.001mm（不悬浮）", (() => {
    const baseY = LIFT + 0 / 1000;
    const pieceY = baseY + 4 / 1000;
    let worst = 0, n = 0;
    const chk = (b, want) => { worst = Math.max(worst, Math.abs(b - want)); n++; };
    sb.root.children.forEach((ch) => {
      const u = ch.userData || {};
      if (u.sortName || u.sortSlot) chk(ch.position.y - 0.014 / 2, pieceY);
      if (u.sortTray) {
        const f = ch.children[0];
        chk(f.position.y - f.geometry.parameters.height / 2, baseY);
      }
    });
    const cam = sb.root.children.find((c) => c.name === "sorting-camera");
    if (cam) chk(cam.position.y + cam.children[0].position.y
      - cam.children[0].geometry.parameters.height / 2, baseY);
    return n >= 6 && worst < 1e-6;
  })());
  ok("★ 镜头朝上（镜头 Y > 机身中心 Y，且机身底面贴地）", (() => {
    const g = sb.root.children.find((c) => c.name === "sorting-camera");
    if (!g || g.children.length < 2) return false;
    const body = g.children[0], lens = g.children[1];
    const base = g.position.y;
    return lens.position.y > body.position.y
      && Math.abs(base + body.position.y - body.geometry.parameters.height / 2 - base) < 1e-9;
  })());

  // ★★ 用户口径：A 区一个盒子装 9 块料、B 区一个盒子 9 个空位（各一个托盘盒）
  ok("★ A/B 各一个托盘盒（共 2 个）", info.trays.length === 2, "实际: " + info.trays.length);
  ok("★ 托盘盒尺寸合理（盒内放得下料：每边比料宽至少 12mm）",
    info.trays.length === 2 && info.trays.every((t) => t[0] > 70 + 24 && t[1] > 70 + 24
      && Math.abs(t[2] - (14 + 6)) < 1e-9),
    JSON.stringify(info.trays));
  ok("★ 托盘盒底板底面贴台面（底板上表面 = 台面+板厚，不漂浮）", (() => {
    const tr = sb.root.children.filter((c) => c.userData && c.userData.sortTray);
    return tr.length === 2 && tr.every((g) => {
      const f = g.children[0];
      const bottomMm = (f.position.y - f.geometry.parameters.height / 2 - LIFT) * 1000;
      return Math.abs(bottomMm - 0) < 1e-6;
    });
  })());
  ok("★ 盒壁高过料（料不会冒出盒口）", (() => {
    const tr = sb.root.children.filter((c) => c.userData && c.userData.sortTray);
    return tr.length === 2 && tr.every((g) => {
      const wallTopMm = (g.children[1].position.y + g.children[1].geometry.parameters.height / 2
        - LIFT) * 1000;
      return wallTopMm - 0 >= 14;
    });
  })());
  // ★ 踩过的坑：托盘盒 Group 放在原点（子网格带绝对坐标），于是
  //   `group.position` **不是**盒中心 —— 拿它算"两盒撞不撞"会得出"中心重合、撞了"。
  //   中心只能从 userData.centerMm 读，这里把它钉住。
  ok("★ 托盘盒 Group 在原点（子网格带绝对坐标，这是既定设计）", (() => {
    const tr = sb.root.children.filter((c) => c.userData && c.userData.sortTray);
    return tr.length === 2 && tr.every((g) => Math.abs(g.position.x) < 1e-9
      && Math.abs(g.position.z) < 1e-9);
  })());
  ok("★ 盒中心 = 该区槽位中心（不是 Group 原点）", (() => {
    const tr = sb.root.children.filter((c) => c.userData && c.userData.sortTray);
    if (tr.length !== 2) return false;
    const xs = tr.map((g) => g.userData.centerMm[0]).sort((a, b) => a - b);
    const wantA = (335.56 + 331.65) / 2;      // A 区（X 小）
    const wantB = (433.40 + 433.80) / 2;      // B 区（X 大）
    return Math.abs(xs[0] - wantA) < 0.01 && Math.abs(xs[1] - wantB) < 0.01;
  })());

  // 共面：所有"槽位节点"（A 方块 / B 底框 / B 方块）的 Y 必须同一个值
  //       （料底面贴托盘内底面 → 中心 = 台面 + 托盘板厚 + 半厚）；摄像头/手上那块不算
  const ys = [];
  sb.root.children.forEach((ch) => {
    const u = ch.userData || {};
    if (u.sortName || u.sortSlot || u.sortSocket) ys.push(ch.position.y);
  });
  const want = (0 + 4) / 1000 + LIFT + 0.014 / 2;
  ok(`所有槽位同高、且都坐在托盘里（不漂浮，Y=${want.toFixed(4)}，共 ${ys.length} 个节点）`,
    ys.length >= 6 && ys.every((y) => Math.abs(y - want) < 1e-6));

  // 重叠的槽位不画
  const sb2 = createSortingBlocks(LIFT);
  sb2.setSlots({ ...PAYLOAD, overlaps: [{ a: "B1", b: "B4", dist: 20, need: 31.5 }] });
  const i2 = sb2.info();
  ok("重叠风险的槽位不画（B1/B4 都跳过）",
    !i2.b.includes("B1") && !i2.b.includes("B4") && i2.warned.some((w) => w.includes("重叠")));
  sb2.dispose();

  console.log("");
  console.log("== C. 状态映射（A 消失 / 手上 / 标红 / B 落位） ==");
  // C1 取料中（还没吸上）：A1 仍在
  sb.update({ piece: 1, holding: false, placement: [], color: "", stage: "取料·下落" });
  const cellOf = (n) => sb.root.children.find((c) => c.userData && c.userData.sortName === n);
  // 用 info 的可见性判定：这里直接按名字找 mesh 的可见性
  const aVis = {};
  sb.root.children.forEach((ch) => {
    if (ch.type !== "Group" || !ch.children.length) return;
    aVis[ch.name || ""] = ch.visible;
  });
  ok("取料中：A1 仍在（还没吸上）", sb.info().a.includes("A1"));

  // C2 吸上了（holding）：已放走 0 件 → A1 该消失（在手上）
  sb.update({ piece: 1, holding: true, placement: [], color: "红", stage: "抬起" });
  ok("吸合后：手上出现方块", sb.info().hand === true);

  // C3 放走 1 件、第 2 件在手上 → A1 与 A2 都不在场景里（A1 已放走、A2 在手上）
  sb.update({ piece: 2, holding: true, color: "绿", stage: "送检",
              placement: [{ color: "红", used: ["B1"], filled: 1, capacity: 3, full: false },
                          { color: "绿", used: [], filled: 0, capacity: 3, full: false },
                          { color: "蓝", used: [], filled: 0, capacity: 3, full: false }] });
  const hidden = [];
  sb.root.children.forEach((ch) => { if (ch.userData && ch.userData.sortName && !ch.visible) hidden.push(ch.userData.sortName); });
  ok("已放走 + 在手上的格子都隐藏（A1、A2）",
    hidden.includes("A1") && hidden.includes("A2"), "实际隐藏: " + JSON.stringify(hidden));

  // C4 失败停止（疑似没取到）：该格画回来，且材质换成警示红
  sb.update({ piece: 2, holding: false, suspect_pick: true, finished: true, ok: false,
              error: "检测不到", placement: [{ color: "红", used: ["B1"], filled: 1, capacity: 3, full: false }] });
  let suspectName = null, suspectHex = null;
  sb.root.children.forEach((ch) => {
    if (ch.userData && ch.userData.sortName && ch.visible && ch.userData.suspect) suspectName = ch.userData.sortName;
    if (ch.userData && ch.userData.sortName && ch.visible
        && ch.children[0] && ch.children[0].material
        && ch.children[0].material.color
        && ch.children[0].material.color.getHex() === HEX_SUSPECT) {
      suspectHex = ch.userData.sortName;
    }
  });
  ok("疑似没取到 → 方块画回 A 格（不留空）", suspectName === "A2", "实际: " + suspectName);
  ok("疑似没取到 → 该方块标红（警示色，不是黑）", suspectHex === "A2", "实际: " + suspectHex);
  ok("失败停止后手上不显示夹持", sb.info().hand === true);   // 对象存在但 visible=false
  const handGroup = sb.root.children.find((c) => c.userData && c.userData.sortHand);
  ok("手上那块 visible=false（不显示吸住）", !!(handGroup && handGroup.visible === false));

  // C5 人工停止（cancelled）：件留在手上
  sb.update({ piece: 3, holding: true, color: "蓝", finished: true, ok: false, phase: "cancelled" });
  const hg = sb.root.children.find((c) => c.userData && c.userData.sortHand);
  ok("人工停止：件留在手上（如实显示还夹着）", !!(hg && hg.visible === true));

  // C6 B 区落位：颜色映射正确
  sb.update({ placement: [{ color: "红", used: ["B1"], filled: 1, capacity: 3, full: false },
                          { color: "绿", used: ["B4"], filled: 1, capacity: 3, full: false }] });
  const bColr = {};
  sb.root.children.forEach((ch) => {
    if (ch.userData && ch.userData.sortSlot) bColr[ch.userData.sortSlot] = ch.visible ? ch.userData.colorName : "";
  });
  ok("B1 显示为红色实心块", bColr.B1 === "红", "实际: " + bColr.B1);
  ok("B4 显示为绿色实心块", bColr.B4 === "绿", "实际: " + bColr.B4);
  // ★ 2026-10-10：现场口径 = **四色**，其中 红/绿/蓝 为**有效色**、黑为**无效色**。
  //   四色必须齐全且互不相同（否则"3D 里的红"会和识别出的"红"不是一回事）。
  const colorNames = Object.keys(SORT_COLOR_HEX);
  ok("四色映射齐全（红/绿/蓝 有效 + 黑 无效）且互不相同",
    colorNames.length === 4 && ["红", "绿", "蓝", "黑"].every((n) => !!SORT_COLOR_HEX[n])
    && new Set(Object.values(SORT_COLOR_HEX)).size === 4);
  // ★★ 2026-10-11 修复（颜色分叉）：3D 的「绿」原来与**权威色卡**不是同一个绿，
  //   而本表注释自称"与视觉/后端同名同色"。现与 camera/vision_color.py 的 #1FA74A 对齐；
  //   前端流程色标 SortFlow.vue 也必须是同一个值（三处同色，改一处必须同步）。
  ok("★★ 3D 的「绿」与权威色卡/流程色标同一值（#1FA74A）",
    SORT_COLOR_HEX["绿"] === 0x1fa74a
    && /"绿":\s*"#1FA74A"/.test(read("src/components/SortFlow.vue"))
    && /1FA74A/i.test(read("../camera/vision_color.py")));

  // C7 空跑：占位色不冒充真实判色
  sb.update({ placement: [], holding: true, color: "红", sim_color: true, dry_run: true });
  const hg2 = sb.root.children.find((c) => c.userData && c.userData.sortHand);
  ok("空跑占位色 → 手上那块用未知灰（不编颜色）",
    !!(hg2 && hg2.children[0].material.color.getHex() !== SORT_COLOR_HEX["红"]));

  // ★★ 2026-10-09（用户："吸气的时候 3D 物体贴着机器人的 J6 那个工具的红色吸盘
  //    一起走，不要消失"）：手上那块必须挂在**吸盘口锚点**上，而不是法兰中心。
  //
  //    旧实现把料放在"法兰中心正下方 4mm"，而本工具从法兰沿 z 伸出 132mm、
  //    两个红色吸盘在左右立板外侧（开口朝 ±x）→ 法兰中心那个位置**在腕部实体内部**，
  //    料被腕筒挡住 = 现场看到的"一吸气工件就消失"。
  //
  //    判据（都对着 endEffector 的几何算，不是拍脑袋）：
  //      ① 料中心必须在**吸盘外侧**（沿锚点 +x 走半个料厚），不能在法兰轴上；
  //      ② 料的**厚度方向**要对齐吸盘开口（局部 y 转到锚点 x）—— 否则料"竖着插进去"；
  //      ③ 锚点缺失时必须退回旧位置兜底（绝不把料丢在原点不动）。
  console.log("");
  console.log("== F. 手上工件贴吸盘（跟 J6 吸盘一起走，不消失） ==");
  {
    const sbF = createSortingBlocks(LIFT);
    sbF.setSlots(PAYLOAD);
    sbF.update({ piece: 1, holding: true, color: "红", placement: [] });
    const hg = sbF.root.children.find((c) => c.userData && c.userData.sortHand);
    ok("手上那块已创建且可见", !!(hg && hg.visible === true));

    // 构造一个"吸盘口锚点"：位置随意（模拟已随 J6 移动），朝向取一个非平凡值
    const q = new THREE.Quaternion().setFromEuler(new THREE.Euler(0.3, -0.7, 1.1));
    const anchor = { x: 1000, y: 2000, z: 3000, q: [q.x, q.y, q.z, q.w] };
    sbF.step(0.016, { x: 999, y: 1999, z: 2999 }, anchor);

    // ★ 2026-10-10：工件吸在**下面那只吸盘**（−x 侧开口），料心沿 −x 外移半个料厚。
    const out = new THREE.Vector3(-1, 0, 0).applyQuaternion(q);     // 锚点 −x 世界方向（下方开口）
    const halfThickMm = (70 * 0 + 0.014) * 1000 / 2;                // 料厚 14mm → 半厚 7mm
    const want = new THREE.Vector3(
      (anchor.x + out.x * halfThickMm) / 1000,
      (anchor.y + out.y * halfThickMm) / 1000,
      (anchor.z + out.z * halfThickMm) / 1000,
    );
    const d = hg.position.distanceTo(want) * 1000;                  // mm
    ok(`★ 料心落在吸盘口外侧（沿开口方向偏半个料厚，实测偏差 ${d.toFixed(3)}mm）`, d < 0.01);

    // ② 厚度方向对齐：料的局部 y 轴（厚 14mm）在世界里应平行于锚点 x 轴（开口方向，±x 均可）
    const q2 = hg.quaternion.clone();
    const handY = new THREE.Vector3(0, 1, 0).applyQuaternion(q2);
    ok("★ 料的厚度方向对齐吸盘开口（不是竖着插进吸盘）",
      Math.abs(Math.abs(handY.dot(out)) - 1) < 1e-6,
      "|dot|=" + Math.abs(handY.dot(out)).toFixed(4));

    // ③ 反证：锚点缺失 → 退回兜底位置，且**仍在机器人附近**（不许丢到原点/不动）
    sbF.step(0.016, { x: 458.8, y: 746.5, z: 0 }, null);
    const fallbackMm = hg.position.clone().multiplyScalar(1000);
    ok("★ 反证：锚点未就绪时退回法兰附近兜底（不消失、不丢在原点）",
      Math.abs(fallbackMm.x - 458.8) < 1 && Math.abs(fallbackMm.y - 746.5) < 20,
      `fallback=(${fallbackMm.x.toFixed(1)}, ${fallbackMm.y.toFixed(1)})`);

    sbF.dispose();
  }

  // ★★ 2026-10-10 用户口径：J6 是**两块板**（下板 −x / 上板 +x），一趟两面各吸一件
  //    （方案 docs/方案-J6双面吸盘（一趟两件·靠J6翻转用另一面）.md §3/§4）。
  //    这里钉死三件事：① 双面同时给料 → **两块**都可见、face 分 lower/upper；
  //    ② 只给旧 `holding` → 仍只有一块（不回归）；③ 两块位置**沿工具局部 x 分居两侧**。
  console.log("");
  console.log("== G. 双面两块料（一趟两件：下板 + 上板） ==");
  {
    const sbG = createSortingBlocks(LIFT);
    sbG.setSlots(PAYLOAD);
    // 两块同时给（不同色，且上板那件是**无效色**「黑」）—— 两件各取各色
    sbG.update({ piece: 2, holding: true, sim_color: false,
                 hold_lower: { color: "红", ok: true },
                 hold_upper: { color: "黑", ok: false },
                 hold_inferred: true });

    const hands = sbG.root.children.filter((c) => c.userData && c.userData.sortHand);
    const lower = hands.find((c) => c.userData.face === "lower");
    const upper = hands.find((c) => c.userData.face === "upper");
    ok("双面给料 → 手上出现**两块**", hands.length === 2, "实际: " + hands.length);
    ok("两块 userData.face 分别为 lower / upper", !!lower && !!upper);
    ok("两块都可见（同时显示）", !!(lower && lower.visible && upper && upper.visible));
    ok("★ 「哪面在吸」如实标注为流程推断（userData.inferred === true）",
      !!(lower && lower.userData.inferred === true && upper && upper.userData.inferred === true));
    ok("两块各取各色：下板=红、上板=无效色黑",
      !!(lower && lower.children[0].material.color.getHex() === SORT_COLOR_HEX["红"]
         && upper && upper.children[0].material.color.getHex() === SORT_COLOR_HEX["黑"]),
      `下=${lower && lower.children[0].material.color.getHex().toString(16)}`
      + ` 上=${upper && upper.children[0].material.color.getHex().toString(16)}`);

    // 摆到吸盘口：两块必须**沿工具局部 x 分居两侧**，且下板那块仍在原有位置
    const qG = new THREE.Quaternion().setFromEuler(new THREE.Euler(0.2, 0.9, -0.4));
    const anchor = { x: 500, y: 800, z: 1200, q: [qG.x, qG.y, qG.z, qG.w] };
    sbG.step(0.016, { x: 500, y: 800, z: 1200 }, anchor);
    const pL = lower.position.clone(), pU = upper.position.clone();
    const outG = new THREE.Vector3(-1, 0, 0).applyQuaternion(qG);   // 下板开口方向（−x）
    const dLU = new THREE.Vector3().subVectors(pL, pU);
    ok("★ 两块位置确实不同（沿工具局部 x 分居两侧）",
      dLU.length() > 0.001 && Math.abs(dLU.clone().normalize().dot(outG)) > 0.999,
      "间距(mm)=" + (dLU.length() * 1000).toFixed(2));
    const halfG = (70 * 0 + 0.014) * 1000 / 2;                      // 料厚 14 → 半厚 7mm
    const wantL = new THREE.Vector3((anchor.x + outG.x * halfG) / 1000,
                                    (anchor.y + outG.y * halfG) / 1000,
                                    (anchor.z + outG.z * halfG) / 1000);
    ok("下板那块仍在原有位置（贴下板吸盘口外半厚，未回归）",
      pL.distanceTo(wantL) * 1000 < 0.01);
    // 上板那块在**另一半**（下板开口方向的相反侧），且更靠外多出「两口间距」
    const relU = new THREE.Vector3().subVectors(pU, pL).normalize();
    ok("上板那块在下板的**另一侧**（−x 开口的相反方向 = +x）",
      relU.dot(outG) < -0.999);
    sbG.dispose();

    // ★ 兼容回归：state 里只有旧的 `holding`（单件）→ 只有一块、且是下板、上板不显示
    const sbH = createSortingBlocks(LIFT);
    sbH.setSlots(PAYLOAD);
    sbH.update({ piece: 1, holding: true, color: "蓝", placement: [] });
    const visH = sbH.root.children.filter((c) => c.userData && c.userData.sortHand && c.visible);
    ok("只给旧 holding → 仍只有**一块**可见（不回归）",
      visH.length === 1 && visH[0].userData.face === "lower",
      "可见块数=" + visH.length);
    // 双面字段为 null（后端未给料）→ 两块都不显示（不会凭空多出一块）
    sbH.update({ piece: 0, holding: false, hold_lower: null, hold_upper: null });
    const visH2 = sbH.root.children.filter((c) => c.userData && c.userData.sortHand && c.visible);
    ok("hold_lower/hold_upper 都为 null → 手上一块都不显示", visH2.length === 0);
    sbH.dispose();
  }

  console.log("");
  console.log("== D. 资源释放 ==");
  sb.dispose();
  ok("dispose 后根节点无子节点", sb.root.children.length === 0);
  ok("dispose 后自检信息清空", sb.info().a.length === 0 && sb.info().b.length === 0);
  ok("dispose 可重复调用（幂等）", (() => { sb.dispose(); return sb.root.children.length === 0; })());
}

console.log("");
console.log("== E. 「每次运行必须从头开始」的界面契约（源码扫描） ==");
{
  const view = read("src/components/SortingView.vue");
  const store = read("src/stores/ai.js");
  const api = read("../backend/app/api/sorting.py");
  const svc = read("../backend/app/services/sorting.py");

  // 后端：初始状态必须由 fresh_run_state 统一构造（残留字段无处藏身）
  ok("后端有 fresh_run_state() 统一构造每轮初始状态",
    /def fresh_run_state\(/.test(svc));
  ok("初始状态里进度/占位/颜色/原因/夹持全部归零",
    /"piece": 0/.test(svc) && /placement_summary\(new_placement\(\)\)/.test(svc)
    && /"error": ""/.test(svc) && /"holding": False/.test(svc)
    && /"suspect_pick": False/.test(svc));
  ok("分拣 /start 用 fresh_run_state（不是就地拼字段）",
    /fresh = S\.fresh_run_state\(/.test(api));
  // ★★ 关键：启动响应必须是**启动前的快照**（活动字典会被线程改掉）
  ok("★ /start 回快照（deepcopy）而不是活动 state 对象",
    /snap = _copy\.deepcopy\(fresh\)/.test(api) && /"state": snap/.test(api));
  // ★ 2026-10-09：判据从"字面量"改成"语义" —— 启动快照后来抽成了 `_snap()` 函数
  //   （为了与"接管接力"共用同一处），但**语义必须不变**：
  //   启动响应回的是**线程启动前**的深拷贝，不是活动字典本身。
  const aiSrc = read("../backend/app/api/ai.py");
  ok("AI /exec 同样回快照（深拷贝，不是活动字典）",
    /snap = _snap\(st\)/.test(aiSrc)
    && /def _snap\(st: Dict\)[\s\S]{0,260}deepcopy/.test(aiSrc));
  ok("引擎每轮从 A1 起（A_SLOTS 顺序固定）",
    /for idx, a_slot in enumerate\(S\.A_SLOTS\[: int\(body\.max_pieces\)\], start=1\)/.test(api));

  // 前端：拿到快照后必须**立刻**复位 3D（不能等下一次轮询）
  ok("★ 分拣页点「开始」后立刻把快照喂给 3D（不等轮询）",
    /updateSortingState\(st\.value\);/.test(view)
    && /本轮从头开始/.test(view));
  ok("分拣页复位日志游标（新一轮阶段变化重新记）",
    /_logKey = ""; _logPrev = \{\};/.test(view));
  ok("AI 页同样复位日志游标并写明「本轮从头开始」",
    /this\._lastLogKey = ""/.test(store) && /本轮从头开始/.test(store));

  // ★★ 用户口径（2026-10-08 第 2 轮）："真实监控／模拟仿真／点位执行／程序执行／
  //    分拣／AI 执行**全部要显示**" —— 3D 的装填与轮询必须是 App 级、且不再被隐藏。
  const app = read("src/App.vue");
  const s3ds = read("src/stores/sorting3d.js");
  /** 去掉整行注释再判断"有没有这个调用" —— 注释里写"以前是 showSortingBlocks(false)"
   *  不该被当成还在调用（否则测试逼着人删掉解释历史的注释，那是本末倒置）。 */
  const strip = (src) => src.split("\n").filter((l) => !l.trim().startsWith("//")).join("\n");
  ok("★ 3D 装填收敛到全局 store（不再只由分拣页挂载时拉一次）",
    /useSorting3dStore/.test(app) && /setSortingSlots/.test(s3ds));
  ok("★ 拿到控制令牌即装填；无令牌不打（否则必然 401 刷屏）",
    /auth\.controlActive/.test(s3ds) && /需要控制令牌/.test(s3ds));
  ok("★ 装填前还问一句「3D 对象是否真在」（disposeAll 后 loaded 仍为 true 会永不重建）",
    /sortingBlocksReady\(\)/.test(s3ds) && /sortingBlocksReady/.test(
      read("src/three/manager.js")));
  ok("★★ 分拣页离开时**不再隐藏** 3D（各页常显）",
    !/showSortingBlocks\(false\)/.test(strip(view)) && !/showSortingBlocks\(false\)/.test(strip(s3ds)));
  ok("★ 非分拣页由 store 轮询喂状态；分拣页自己不重复打",
    /robot\.activeView === "sort"/.test(s3ds) && /updateSortingState/.test(s3ds));
  // ★★ 2026-10-10 用户口径："AI 执行没有变颜色…**所有的执行都要联动**"。
  //   3D 原来只喂 `/sorting/state` —— AI 执行时那个口是空的，于是 3D 一动不动。
  //   现在改喂后端**统一场景状态** `/control/scene-state`（返回"当前在跑的那一个执行"）。
  ok("★★ 3D 喂的是**统一场景状态**（任何执行在跑都能联动，不再只认分拣口）",
    /\/control\/scene-state/.test(s3ds) && !/apiControl\("\/sorting\/state"\)/.test(s3ds));
  ok("★★ 后端提供 /control/scene-state，并登记分拣 / AI 两个场景源",
    /def api_scene_state/.test(read("../backend/app/api/control.py"))
    && /register_scene_source\("sorting"/.test(read("../backend/app/api/sorting.py"))
    && /register_scene_source\("ai"/.test(read("../backend/app/api/ai.py")));
  ok("★ 隐藏标签页/无令牌时停轮询（与渲染帧同一个闸门）",
    /s3d\.stop\(\)/.test(app) && /s3d\.start\(\)/.test(app));
  ok("★ 示教/清空点位后重算摆位（否则盒子与料还停在旧位置）",
    /s3d\.reload\(\)/.test(view) && /async reload\(\)/.test(s3ds));
  ok("★ 后端方块尺寸默认 70mm（不是 30mm）且已说明受实测列距限制",
    /BLOCK_MM_DEFAULT = \(70\.0/.test(api) && /85/.test(api));
  ok("★ 后端相机高度默认 0（坐在地面），前端不再叠加示教点 Z",
    /CAMERA_H_MM_DEFAULT = 0\.0/.test(api) && !/cam\.pos\[2\] \+ h/.test(
      read("src/three/sortingBlocks.js")));

  // ★★ 用户 2026-10-08 第三轮：吸气全程保持 + 到 B 区「停止吸 → 放气 0.5s → 停止放气」
  //    "顺序即工艺"，用源码位置钉死（引擎要真机才能整跑，只能这样断言顺序）。
  //    ★ 必须只在**主管线切片**里找位置：文件头的流程注释里也有这些词，
  //      全文 indexOf 会指到注释上，得出"回起点在第 367 字"这种假位置。
  const loop = api.slice(api.indexOf("for idx, a_slot in enumerate"));
  const iSuck = loop.indexOf('_air(ctx, "suck"');
  const iRel = loop.indexOf('_air(ctx, "release"');
  const iBlow = loop.indexOf('_air(ctx, "blow"');
  const iUnblow = loop.indexOf('_air(ctx, "unblow"');
  const iAtB = loop.indexOf("放料·正上方");
  // ★ 锚"真实调用"而不是词：注释里也会出现"回起点"（解释历史写法），锚词会误判。
  const iBack = loop.indexOf('_move(ctx, pmap, start, speed, dry, "回起点")');
  ok("★ 吸气全程保持：`release` 不在取料之后（在放料段里才断）",
    iSuck > 0 && iRel > iSuck && iAtB > 0 && iRel > iAtB,
    `suck@${iSuck} release@${iRel} 放料@${iAtB}`);
  ok("★ 到 B 区先停止吸、再放气（release 在 blow 之前）", iRel > 0 && iRel < iBlow);
  // ★★ 2026-10-11：缺省值改从**工艺时序唯一来源** `services/sorting.py::BLOW_MS` 取（值仍是 500ms）
  ok("★ 放气持续时间取自 blow_ms、缺省 = S.BLOW_MS = 500ms",
    /blow_ms: int = Field\(default=S\.BLOW_MS/.test(api) && /int\(body\.blow_ms\)/.test(api)
    && /^BLOW_MS = 500/m.test(svc));
  ok("★★ 放气**就地**停止（unblow 在回起点之前，不再保持到回起点）",
    iUnblow > iBlow && iBack > iUnblow, `unblow@${iUnblow} 回起点@${iBack}`);
  ok("★★ 异常/取消必须收掉两路气（suck_on 兜底，不许带着负压停在半路）",
    /suck_on = True/.test(api) && (api.match(/suck_on = False/g) || []).length >= 3
    && (api.match(/if not dry and \(blow_on or suck_on\)/g) || []).length >= 3
    && /\(("|')unblow("|'), ("|')停止放("|')\)/.test(api)
    && /\(("|')release("|'), ("|')停止吸("|')\)/.test(api));
  ok("★ 前端参数默认与后端一致（放气时长 500ms，标签不再是「放气后停留」）",
    /const blowMs = ref\(500\)/.test(view) && /放气时长/.test(view)
    && !/放气后停留/.test(view));
}

console.log("");
console.log("结果: " + pass + " 通过 / " + fail + " 失败");
process.exit(fail ? 1 : 0);
