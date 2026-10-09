// =====================================================================
// 分拣 3D 可视化：18 个槽位方块 + C 检测工位摄像头模型（跟随执行同步）
//
// 方案：docs/方案-分拣3D可视化（18方块跟随执行同步）.md
//
// 数据来源（**不新增状态机**）：
//   ① 摆位：后端 `GET /api/sorting/blocks` —— 由示教点位经 FK 算出基座系 mm，
//      这里用 `sceneCoords.sceneFromBaseMm()` 换成场景坐标（**位置按示教点位示意，
//      不是相机识别的实际位置**，界面必须照此标注）。
//   ② 状态：既有 `GET /api/sorting/state`（piece / holding / color / placement /
//      suspect_pick / dry_run …）。
//
// 三条现场口径（用户 2026-10-08）：
//   · 吸不住 → 3D 也**不显示吸住**：失败停止时把方块**画回 A 格并标红**（"疑似没取到"）。
//   · 人工「停止」→ 件可能还在吸盘上 → **如实留在手上**，不自动回位（回位就是编故事）。
//   · 全部在平面上、不漂浮、不重叠：方块底面统一贴 `plane_z`；重叠的槽位**不画**。
//
// 现场口径补充（用户 2026-10-08 第 2 轮）：
//   · **A 区 = 一个大托盘盒，里面 9 块矩形料**；**B 区 = 一个大托盘盒，里面 9 个空位**。
//     托盘盒底面同样贴摆放面（和料一个平面，绝不漂浮）。
//   · **摄像头摆在平面（台面/地面）上，镜头朝上**（不是吊在半空往下拍）。
//   · 料要"大一点点"→ 由后端 `block_mm` 下发（70mm，受实测列距 85~105mm 限制，
//     再大就会互相压住，重叠检测会判定为不可摆）。
//
// ★★ 高度口径（用户 2026-10-08 第 3 轮："19 个摆放的 3D 物体怎么悬浮起来了啊，
//    放在地上啊"）—— 这是我上一轮的错，记在这里以免再犯：
//    示教点存的是关节角，FK 算出来的是**法兰中心**（工具安装面），**不是料的表面**；
//    料在工具下方，低着"一个末端工具长度"。上一轮把 `plane_z`(149.9mm) 当成料高，
//    于是料被画到离地 330mm（比站台台面还高 150mm）→ 看着就是悬空。
//    正确做法：底面统一坐在 `plane_h_mm`（默认 0 = 站台台面 = 机器人底座平面）。
//    佐证：A1 与 A1上 只差 34.3mm 且纯竖直（抬升避让，说明料厚 ≤34mm）；
//          A/B/C 实测 X 285~578 / Y ±446mm 全在站台台面范围内（±655mm）。
//    `plane_z` 仍然收下，但**只用于体检显示**（示教共面偏差），不再参与摆放高度。
//
// ⚠ 这一切都是**按流程推断**（现场没有真空反馈），不是传感器实测 —— 界面已注明。
// =====================================================================
import * as THREE from "three";
import { sceneFromBaseMm } from "./sceneCoords.js";

/** 三色 → 十六进制（与视觉/后端同名同色，避免"3D 里的红"与识别出的"红"不是一回事）。 */
export const SORT_COLOR_HEX = { "红": 0xe02020, "绿": 0x2e9e4f, "蓝": 0x1f4fd8 };
/** 未判色 = 未知灰；疑似没取到 = 警示红（与"红方块"区分：更亮 + 描边）。 */
export const HEX_UNKNOWN = 0x8a8f95;
export const HEX_SUSPECT = 0xff3b30;
const HEX_EMPTY = 0xdfe3e8;             // B 区空槽底框（灰白）
const HEX_CAM_BODY = 0x2b3038;
const HEX_LENS = 0x11151a;
const HEX_BLOCK_EDGE = 0x0b0e11;        // 方块描边（深色，让蓝色块与蓝色残影区分开）

/** 方块默认尺寸(mm) —— 与后端 `BLOCK_MM_DEFAULT` 保持一致（现场可传参覆盖）。 */
export const DEFAULT_BLOCK_MM = [70, 70, 14];

/** 托盘盒参数(mm)：内净空到料的间隙 / 壁厚 / 壁高 = 料厚 + 该值。 */
//  ★ 托盘与料的高度关系（物理正确版）：**托盘底面坐在摆放面上，料的底面坐在托盘内底面上**
//    （即料比摆放面高一个 `TRAY_FLOOR_H`）。第一版把"板的顶面"当摆放面 → 板厚 4mm 沉到
//    台面以下，落地检查会报 4mm 偏差。
const TRAY_GAP = 6, TRAY_WALL = 6, TRAY_FLOOR_H = 4, TRAY_WALL_OVER = 6;

function disposeMat(m) {
  try { if (m && m.dispose) m.dispose(); } catch (e) { /* 忽略 */ }
}
function disposeGeo(g) {
  try { if (g && g.dispose) g.dispose(); } catch (e) { /* 忽略 */ }
}

/**
 * 建一个分拣可视化对象。
 * @param {number} liftY 场景里机器人立足面高度(米)，与 manager.baseLiftY() 同源
 */
export function createSortingBlocks(liftY = 0) {
  const root = new THREE.Group();
  root.name = "sorting-blocks";

  // 共享资源（一次性建、统一释放；每种颜色一份材质，不按槽位重复建）
  const edgeGeos = [];     // 方块描边几何（每个方块一份 clone）
  const mats = [];
  const mkMat = (m) => { mats.push(m); return m; };

  const matBlack = mkMat(new THREE.MeshLambertMaterial({ color: 0x14181c }));
  const matByColor = {};
  for (const [name, hex] of Object.entries(SORT_COLOR_HEX)) {
    matByColor[name] = mkMat(new THREE.MeshLambertMaterial({ color: hex }));
  }
  const matUnknown = mkMat(new THREE.MeshLambertMaterial({ color: HEX_UNKNOWN }));
  const matSuspect = mkMat(new THREE.MeshLambertMaterial({ color: HEX_SUSPECT }));
  const matEdge = mkMat(new THREE.LineBasicMaterial({ color: HEX_BLOCK_EDGE }));
  const matSocket = mkMat(new THREE.LineBasicMaterial({ color: HEX_EMPTY }));
  const matCamBody = mkMat(new THREE.MeshLambertMaterial({
    color: HEX_CAM_BODY, transparent: true, opacity: 0.55 }));
  const matCamLens = mkMat(new THREE.MeshLambertMaterial({ color: HEX_LENS }));
  // 托盘盒：底板实一点、四壁半透（不然挡住盒里的料）
  const matTrayFloor = mkMat(new THREE.MeshLambertMaterial({
    color: 0x39414b, transparent: true, opacity: 0.85 }));
  const matTrayWall = mkMat(new THREE.MeshLambertMaterial({
    color: 0x3d4650, transparent: true, opacity: 0.30 }));

  let blockGeo = null;      // 方块几何（按尺寸建一次，全体共用）
  let socketGeo = null;     // 空槽/描边线框几何
  let camBodyGeo = null;    // 摄像头机身
  let camLensGeo = null;    // 摄像头镜头
  let camGroup = null;      // C 区摄像头模型
  let camFlash = 0;         // 判色闪烁计时（秒）

  const aBlocks = new Map();   // name -> { mesh, edge }
  const bSockets = new Map();  // name -> LineSegments
  const bBlocks = new Map();   // name -> { mesh, edge }
  const trays = [];            // A/B 两个托盘盒（Group）
  const trayGeos = [];         // 托盘盒几何（每盒尺寸不同，须各自释放）
  let hand = null;             // 手上那块（跟随末端，位置每帧由 manager 喂世界坐标）
  // 「手上那块」的朝向修正：绕自身 z 轴 90°，把料的厚度方向对齐吸盘开口方向（+x）。
  // 建一次复用（每帧新建 Quaternion 会白白产生垃圾对象）。
  const Q_ROT_HAND = new THREE.Quaternion()
    .setFromAxisAngle(new THREE.Vector3(0, 0, 1), Math.PI / 2);

  let slots = [];              // 后端返回的槽位表（已过滤：未示教/重叠不画）
  let planeZ = 0;              // 示教法兰高度(mm，基座系) —— **只用于体检显示**
  let planeH = 0;              // ★ 摆放面高度(mm，基座系)：底面坐这里，0 = 站台台面
  let sizeM = DEFAULT_BLOCK_MM.map((v) => v / 1000);
  let warned = [];             // 画不了的槽位原因（供界面如实提示）
  let lastKey = "";            // 状态签名：只在变化时改场景（每帧 mutate 会闪）
  // ★ 井字棋棋盘覆盖（2026-10-10）：null = 不干预（分拣照旧）；给出时按棋盘
  //   显隐 B 区方块、按 used 隐藏 A 区料。**只是"多显示一份已落子"**，
  //   不改任何分拣字段 —— 对局期间分拣不会跑，两者天然不打架（见方案 §6.3）。
  let tttOverride = null;      // { board:[...9], used:[...], holding:bool }

  /**
   * 摘掉所有已建节点并释放它们独占的几何。
   * ★ 必须摘 **group**（方块 = group{mesh, edge}）—— 只摘 mesh 会把 group/边缘线留在场景里，
   *   表现是"重新加载槽位后旧方块还在"（且 dispose 后根节点仍有子节点）。
   */
  function clearBuilt() {
    [aBlocks, bSockets, bBlocks].forEach((m) => {
      m.forEach((o) => {
        const node = o.group || o.mesh || o;
        if (node.parent) node.parent.remove(node);
      });
      m.clear();
    });
    if (hand) { if (hand.group.parent) hand.group.parent.remove(hand.group); hand = null; }
    if (camGroup) { if (camGroup.parent) camGroup.parent.remove(camGroup); camGroup = null; }
    trays.splice(0).forEach((t) => { if (t.parent) t.parent.remove(t); });
    if (blockGeo) { disposeGeo(blockGeo); blockGeo = null; }
    if (socketGeo) { disposeGeo(socketGeo); socketGeo = null; }
    if (camBodyGeo) { disposeGeo(camBodyGeo); camBodyGeo = null; }
    if (camLensGeo) { disposeGeo(camLensGeo); camLensGeo = null; }
    trayGeos.splice(0).forEach(disposeGeo);
    edgeGeos.splice(0).forEach(disposeGeo);
  }

  /**
   * 建一个区的托盘盒：底板 + 4 面墙，底面贴**摆放面** `planeH`（和料同平面，不漂浮）。
   * 净空 = 槽位跨度 + 半个料 + 间隙，所以 70mm 的料也放得下、不压边。
   * @param {Array} list 该区**已示教**的槽位（含 pos，mm 基座系）
   * @param {number[]} bm 料尺寸 mm
   */
  function mkTray(list, bm) {
    if (!list.length) return;
    const hx = bm[0] / 2, hy = bm[1] / 2;
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    for (const s of list) {
      minX = Math.min(minX, s.pos[0]); maxX = Math.max(maxX, s.pos[0]);
      minY = Math.min(minY, s.pos[1]); maxY = Math.max(maxY, s.pos[1]);
    }
    const cx = (minX + maxX) / 2, cy = (minY + maxY) / 2;
    const ox = (maxX - minX) / 2 + hx + TRAY_GAP + TRAY_WALL;   // 外半宽(mm)
    const oy = (maxY - minY) / 2 + hy + TRAY_GAP + TRAY_WALL;
    const wh = bm[2] + TRAY_WALL_OVER;                          // 壁高(mm)
    const p = sceneFromBaseMm(cx, cy, planeH, liftY);

    const g = new THREE.Group();
    g.name = "sorting-tray";
    // ★ centerMm 是**基座系 mm** 的盒中心：盒 Group 本身放在原点（子网格带绝对坐标），
    //   所以"盒在哪、和另一个盒撞不撞"只能靠这个字段算 —— 别再拿 group.position 当中心。
    g.userData = { sortTray: true, sizeMm: [ox * 2, oy * 2, wh], centerMm: [cx, cy] };

    const fg = new THREE.BoxGeometry(ox * 2 / 1000, TRAY_FLOOR_H / 1000, oy * 2 / 1000);
    trayGeos.push(fg);
    const floor = new THREE.Mesh(fg, matTrayFloor);
    floor.position.set(p.x, p.y + TRAY_FLOOR_H / 2 / 1000, p.z);   // 底板**底面**贴摆放面
    g.add(floor);

    // 4 面墙（两块走满长、两块缩短，避免墙角重叠导致半透明叠色）
    const wdef = [
      [ox * 2, wh, TRAY_WALL, 0, oy - TRAY_WALL / 2],
      [ox * 2, wh, TRAY_WALL, 0, -(oy - TRAY_WALL / 2)],
      [TRAY_WALL, wh, oy * 2 - TRAY_WALL * 2, ox - TRAY_WALL / 2, 0],
      [TRAY_WALL, wh, oy * 2 - TRAY_WALL * 2, -(ox - TRAY_WALL / 2), 0],
    ];
    for (const [w, hh, dd, dx, dy] of wdef) {
      const wg = new THREE.BoxGeometry(w / 1000, hh / 1000, dd / 1000);
      trayGeos.push(wg);
      const m = new THREE.Mesh(wg, matTrayWall);
      // dx 是基座 X、dy 是基座 Y；场景 = (x, y, -baseY)
      m.position.set(p.x + dx / 1000, p.y + wh / 2 / 1000, p.z - dy / 1000);
      g.add(m);
    }
    root.add(g);
    trays.push(g);
  }

  /** 造一个实心方块（带描边）：蓝色块靠描边与"淡蓝残影"区分。 */
  function mkBlock(mat) {
    const g = new THREE.Group();
    const mesh = new THREE.Mesh(blockGeo, mat);
    g.add(mesh);
    const eg = socketGeo.clone();
    edgeGeos.push(eg);
    g.add(new THREE.LineSegments(eg, matEdge));
    return { group: g, mesh };
  }

  /**
   * 设定槽位（来自 `/api/sorting/blocks`）。
   * @param {{slots:Array,plane_h_mm:number,plane_z:number,block_mm:number[],camera:object,overlaps:Array,missing:Array,plane_spread:number,plane_tol:number}} d
   */
  function setSlots(d) {
    if (!d || !Array.isArray(d.slots)) return;
    clearBuilt();
    warned = [];
    slots = d.slots;
    planeZ = Number(d.plane_z) || 0;
    // ★ 摆放面：`plane_h_mm` 缺省按 0（站台台面）—— 老后端不返回该字段时也不能退回 plane_z，
    //   那正是"悬浮 15cm"的病根。宁可坐台面上，也不许凭旧字段把它抬起来。
    planeH = Number(d.plane_h_mm) || 0;
    const bm = Array.isArray(d.block_mm) && d.block_mm.length === 3 ? d.block_mm : DEFAULT_BLOCK_MM;
    sizeM = bm.map((v) => Number(v) / 1000);

    blockGeo = new THREE.BoxGeometry(sizeM[0], sizeM[2], sizeM[1]);
    socketGeo = new THREE.EdgesGeometry(blockGeo);

    // 重叠的槽位**不画**（宁可缺，也不叠在一起骗人）
    const badNames = new Set();
    (d.overlaps || []).forEach((o) => { badNames.add(o.a); badNames.add(o.b); });

    // ★ 托盘盒：A 区一盒装 9 块料，B 区一盒 9 个空位（盒底 = 料的平面，不漂浮）
    const taught = slots.filter((s) => s && s.taught && Array.isArray(s.pos));
    mkTray(taught.filter((s) => s.role === "A"), bm);
    mkTray(taught.filter((s) => s.role === "B"), bm);

    for (const s of slots) {
      if (!s || !s.taught || !s.pos) {
        warned.push(`${s && s.name}：未示教，不画`);
        continue;
      }
      if (badNames.has(s.name)) {
        warned.push(`${s.name}：与相邻槽位重叠风险，不画`);
        continue;
      }
      // ★ 料/空位坐在**托盘内底面**上（比摆放面高一个托盘底板厚）
      const p = sceneFromBaseMm(s.pos[0], s.pos[1], planeH + TRAY_FLOOR_H, liftY);
      if (s.role === "A") {
        const b = mkBlock(matBlack);
        b.group.position.set(p.x, p.y + sizeM[2] / 2, p.z);   // 方块中心 = 底面 + 半厚
        b.group.userData = { sortName: s.name, role: "A", suspect: false };
        root.add(b.group);
        aBlocks.set(s.name, b);
      } else if (s.role === "B") {
        const sock = new THREE.LineSegments(socketGeo, matSocket);
        sock.position.set(p.x, p.y + sizeM[2] / 2, p.z);
        sock.userData = { sortSocket: s.name };
        root.add(sock);
        bSockets.set(s.name, sock);
        const blk = mkBlock(matUnknown);
        blk.group.position.copy(sock.position);
        blk.group.visible = false;
        blk.group.userData = { sortSlot: s.name, role: "B", colorName: "" };
        root.add(blk.group);
        bBlocks.set(s.name, blk);
      }
    }

    // C 检测工位：摄像头模型摆在**摆放面上、镜头朝上**（用户口径）
    // 高度 = planeH + camera_height_mm（0 = 直接坐在台面上）
    // ★ 不再用示教点 Z：那是法兰高度（294mm），拿它当相机高度会让相机悬在半空。
    const cam = d.camera;
    if (cam && cam.taught && cam.pos) {
      const h = Number(d.camera_height_mm) || 0;
      const cp = sceneFromBaseMm(cam.pos[0], cam.pos[1], planeH + h, liftY);
      camGroup = new THREE.Group();
      camGroup.name = "sorting-camera";
      camGroup.userData = { camBaseMm: [cam.pos[0], cam.pos[1], planeH + h] };
      camBodyGeo = new THREE.BoxGeometry(0.06, 0.08, 0.06);
      const body = new THREE.Mesh(camBodyGeo, matCamBody);
      body.position.y = 0.04;              // 机身底面贴平面
      camGroup.add(body);
      camLensGeo = new THREE.CylinderGeometry(0.014, 0.018, 0.03, 20);
      const lens = new THREE.Mesh(camLensGeo, matCamLens);
      lens.position.y = 0.095;             // 镜头叠在机身上方 → 朝上拍
      camGroup.add(lens);
      camGroup.position.set(cp.x, cp.y, cp.z);
      root.add(camGroup);
    } else {
      warned.push("C检测：未示教，不画摄像头模型");
    }

    if (Number(d.plane_spread) > Number(d.plane_tol)) {
      warned.push(`示教点共面性偏差 ${d.plane_spread}mm（>${d.plane_tol}mm）：仅提示示教精度，摆放已统一坐台面`);
    }
    lastKey = "";
  }

  /** 手上那块：跟随末端（位置由 manager 每帧喂世界坐标 mm）。 */
  function ensureHand() {
    if (hand) return hand;
    const b = mkBlock(matUnknown);
    b.group.visible = false;
    b.group.userData = { sortHand: true };
    root.add(b.group);
    hand = b;
    return hand;
  }

  /**
   * 井字棋棋盘覆盖：给出时 B 区按格子显隐方块、A 区按 used 隐藏料。
   * @param {{board?:Array, used?:Array, holding?:boolean}|null} o 传 null 清除覆盖。
   * ★ 只改可视化，不动分拣逻辑；重置 lastKey 让下一次 update() 立刻重画。
   */
  function setTtt(o) {
    tttOverride = (o && typeof o === "object") ? o : null;
    lastKey = "";
  }

  /**
   * 按 state 更新场景（幂等：同状态不重复 mutate）。
   * @param {object} st `/api/sorting/state` 的 state
   */
  function update(st) {
    const s = st || {};
    const tttOn = !!(tttOverride && Array.isArray(tttOverride.board));
    const tttB = tttOn ? tttOverride.board : null;
    const tttUsed = tttOn && Array.isArray(tttOverride.used) ? tttOverride.used : null;
    const placed = tttOn ? 0 : (Array.isArray(s.placement) ? s.placement : [])
      .reduce((n, c) => n + (Number(c.filled) || 0), 0);
    const holding = tttOn ? !!tttOverride.holding : !!s.holding;
    const suspect = tttOn ? false : !!s.suspect_pick;
    const piece = tttOn ? 0 : (Number(s.piece) || 0);
    const colorName = SORT_COLOR_HEX[s.color] ? s.color : "";
    const simColor = !!s.sim_color;
    const key = [placed, holding, suspect, piece, colorName, simColor,
                 s.stage, s.running, s.dry_run,
                 tttB ? tttB.map((v) => v || ".").join("") : "",
                 tttUsed ? tttUsed.join(",") : ""].join("|");
    if (key === lastKey) return;
    lastKey = key;

    // A 区：已放走的 + （在手上的 / 疑似拿走的）都消失
    const goneA = new Set();
    for (let i = 1; i <= placed; i++) goneA.add("A" + i);
    if (holding && placed < 9) goneA.add("A" + (placed + 1));
    // ★ 井字棋：已取用的 A 格同样消失（棋子已被搬到 B 区）
    if (tttUsed) for (const nm of tttUsed) goneA.add(String(nm).trim().toUpperCase());
    // 失败停止（疑似没取到）：该格**画回来**并标红 —— 不带走、不显示夹持
    const suspectCell = (suspect && piece) ? ("A" + piece) : "";
    aBlocks.forEach((b, name) => {
      const idx = Number(String(name).slice(1));
      const taken = goneA.has(name);
      const isSuspect = name === suspectCell;
      b.group.visible = !taken || isSuspect;
      b.group.userData.suspect = isSuspect;
      const hot = isSuspect || (!!s.running && !holding && idx === piece);
      b.mesh.material = isSuspect ? matSuspect : matBlack;
      // 取料中的那格高亮（emissive 让"正在取这一格"一眼可见）
      try {
        const m = b.mesh.material;
        if (m && m.emissive) m.emissive.setHex(hot && !isSuspect ? 0x2b3a52 : 0x000000);
      } catch (e) { /* 忽略 */ }
    });

    // B 区：已占位的槽显示实心彩色块，其余留空槽线框
    // ★ 井字棋覆盖期间：按棋盘占位显示（用未知灰，符号以 2D 为准），不读分拣 placement
    const usedSlot = new Map();      // slot -> 颜色名（井字棋占位记 "__ttt__"）
    if (!tttOn) {
      (Array.isArray(s.placement) ? s.placement : []).forEach((c) => {
        (c.used || []).forEach((nm) => usedSlot.set(nm, c.color));
      });
    } else if (tttB) {
      for (let i = 0; i < 9; i++) {
        if (tttB[i]) usedSlot.set("B" + (i + 1), "__ttt__");
      }
    }
    bBlocks.forEach((blk, name) => {
      const cn = usedSlot.get(name);
      const mat = cn && matByColor[cn] ? matByColor[cn] : matUnknown;
      if (blk.group.userData.colorName !== (cn || "")) {
        blk.group.userData.colorName = cn || "";
        blk.mesh.material = mat;
      }
      blk.group.visible = !!cn;
    });

    // 手上那块：空跑（占位色）或未判色 → 未知灰，**绝不编一个颜色**出来
    const hb = ensureHand();
    hb.group.visible = holding;
    if (holding) {
      hb.mesh.material = (!simColor && colorName) ? matByColor[colorName] : matUnknown;
    }

    // 摄像头判色闪烁
    if (String(s.stage || "").includes("判色")) camFlash = 0.9;
  }

  /**
   * 每帧推进：摄像头闪烁 + 手上方块**贴着吸盘**跟随末端。
   * @param {number} dt 秒
   * @param {{x:number,y:number,z:number}} tcpMm manager.getTcp()（法兰中心，世界 mm）
   * @param {{x:number,y:number,z:number,q:number[]}|null} anchorMm
   *        manager.getPieceAnchor() —— **J6 吸盘口**的世界位置+朝向。
   *        ★ 2026-10-09：以前只用 tcpMm（法兰中心）把料挂在"法兰下方 4mm"，
   *          而工具从法兰沿 z 伸出 132mm、吸盘在左右侧面 → 料埋在腕部实体里，
   *          现象就是"一吸气工件就消失"。现在贴到吸盘口，并跟随工具姿态。
   */
  function step(dt, tcpMm, anchorMm) {
    if (camFlash > 0) {
      camFlash = Math.max(0, camFlash - dt);
      if (camGroup) {
        const on = camFlash > 0 && Math.floor(camFlash * 8) % 2 === 0;
        camGroup.children.forEach((c) => {
          if (c.material && c.material.emissive) {
            c.material.emissive.setHex(on ? 0x2f9bff : 0x000000);
          }
        });
      }
    }
    if (hand && hand.group.visible) {
      if (anchorMm) {
        // ★ 吸盘口锚点：位置取锚点世界坐标，再沿"锚点自身 +x 轴"外移半个料厚
        //   （吸盘开口朝 +x，料贴在口外侧）。
        const q = new THREE.Quaternion(anchorMm.q[0], anchorMm.q[1],
                                       anchorMm.q[2], anchorMm.q[3]);
        const out = new THREE.Vector3(1, 0, 0).applyQuaternion(q);   // 锚点 +x 世界方向
        hand.group.position.set(
          (anchorMm.x + out.x * sizeM[2] * 500) / 1000,
          (anchorMm.y + out.y * sizeM[2] * 500) / 1000,
          (anchorMm.z + out.z * sizeM[2] * 500) / 1000,
        );
        // 料默认是"平躺"的：BoxGeometry(x=长, y=厚, z=宽)。吸盘开口朝锚点 +x，
        // 所以要让**厚度方向（局部 y）对齐锚点 x** → 绕**自身 z 轴**转 90°：
        //   (x,y,z) → (−y, x, z)：原 y(14 厚) → x 方向 ✓，原 x(70) → y ✓，z(70) 不变 ✓
        // ⚠ 别转 y 轴：那是把长度转到 x，"料会竖着插进吸盘"（第一版就写错了）。
        hand.group.quaternion.copy(q).multiply(Q_ROT_HAND);
      } else if (tcpMm) {
        // 兜底（锚点还没建好时）：退回法兰中心下方，保证"至少跟着动、不消失"
        hand.group.position.set(tcpMm.x / 1000,
                                tcpMm.y / 1000 - sizeM[2] / 2 - 0.004,
                                tcpMm.z / 1000);
        hand.group.quaternion.identity();
      }
    }
  }

  function setVisible(v) {
    root.visible = !!v;
  }

  /** 释放：几何/材质/线框统统释放，并从父节点摘除（disposeAll 会调用）。 */
  function dispose() {
    clearBuilt();
    mats.forEach(disposeMat);
    mats.length = 0;
    if (root.parent) root.parent.remove(root);
    slots = [];
    warned = [];
    lastKey = "";
    tttOverride = null;
  }

  /** 自检/回归用：当前画出来的槽位与原因。 */
  function info() {
    return {
      planeZ, planeH, sizeM: sizeM.slice(),
      // ★ 料（含 B 空位线框）的底面高度(mm，基座系) —— 落地检查用这个，别再自己算
      pieceH: planeH + TRAY_FLOOR_H,
      trayFloorH: TRAY_FLOOR_H,
      a: [...aBlocks.keys()], b: [...bSockets.keys()],
      trays: trays.map((t) => (t.userData.sizeMm || []).slice()),
      camera: !!camGroup,
      camBase: camGroup && camGroup.userData.camBaseMm ? camGroup.userData.camBaseMm.slice() : null,
      hand: !!hand,
      warned: warned.slice(), slots: slots.length,
    };
  }

  return { root, setSlots, update, setTtt, step, setVisible, dispose, info };
}
