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
import { suckerLocalMmForSide } from "./endEffector.js";

/** 四色 → 十六进制（与视觉/后端同名同色，避免"3D 里的红"与识别出的"红"不是一回事）。
 *  ★★ 2026-10-11 修复：`绿` 原来是 `0x2e9e4f`，与**权威色卡**（`camera/vision_color.py` 的
 *    `#1FA74A`、前端流程色标 `SortFlow.vue` 的 `#1FA74A`）**不是同一个绿** ——
 *    而本行注释自称"与视觉/后端同名同色"。现改为与色卡一致，止住这处颜色分叉
 *    （井字棋「叉=绿」复用本表，也一并纠正）。
 *  ★ 「黑」= **无效颜色**（第 4 色）：识别到了但不进分拣，原路放回原位。 */
export const SORT_COLOR_HEX = { "红": 0xe02020, "绿": 0x1fa74a, "蓝": 0x1f4fd8, "黑": 0x1c1f24 };
/** 未判色 = 未知灰；疑似没取到 = 警示红（与"红方块"区分：更亮 + 描边）。 */
export const HEX_UNKNOWN = 0x8a8f95;
export const HEX_SUSPECT = 0xff3b30;
/** ★ 井字棋：符号 → 显示色（用户 2026-10-11 口径「**蓝色是圆，绿色是叉**」）。
 *  ★ 复用 `SORT_COLOR_HEX` 的「蓝/绿」，避免出现两种不一样的蓝/绿。
 *  ★ 这是**棋子身份色**（圆 O / 叉 X），**不是**该格料的实测颜色（自证模式判出的红/绿/蓝）——
 *    默认模式根本不判色，没有实测色可显示（口径见 docs/方案-井字棋3D棋子视觉与复原…§2A）。 */
export const TTT_SYM_HEX = { O: SORT_COLOR_HEX["蓝"], X: SORT_COLOR_HEX["绿"] };
const HEX_MARK = 0xeaf2fb;             // 顶面符号标记（圆环/十字）的亮色

/** A 区来源格 → 它代表的符号（A1–A5 = 圆 O、A6–A9 = 叉 X）。
 *  与后端 `services/tictactoe.py::O_SOURCES/X_SOURCES` **同口径**（改一处必须同步另一处）。 */
export function tttSymForASlot(name) {
  const m = /^A(\d+)$/.exec(String(name || "").trim().toUpperCase());
  if (!m) return "";
  const n = Number(m[1]);
  if (n >= 1 && n <= 5) return "O";
  if (n >= 6 && n <= 9) return "X";
  return "";
}

/** 落子「坠落」动画（纯视觉，**不参与任何安全/碰撞判定**）。 */
const FALL_H_MM = 60;                  // 从格子上方 60mm 处落下
const FALL_DUR = 0.32;                 // 下落时长（秒）
const HEX_EMPTY = 0xdfe3e8;             // B 区空槽底框（灰白）
const HEX_CAM_BODY = 0x2b3038;
const HEX_LENS = 0x11151a;
const HEX_BLOCK_EDGE = 0x0b0e11;        // 方块描边（深色，让蓝色块与蓝色残影区分开）

/** 方块默认尺寸(mm) —— 与后端 `BLOCK_MM_DEFAULT` 保持一致（现场可传参覆盖）。 */
export const DEFAULT_BLOCK_MM = [70, 70, 14];

// ★ 双面两块料（2026-10-10 · 方案 docs/方案-J6双面吸盘（一趟两件·靠J6翻转用另一面）.md §3/§4）：
//   下板(−x) 与上板(+x) 的吸盘口沿工具局部 x 关于法兰轴**对称**，两口间距 =
//   suckerLocalMmForSide(+1) − suckerLocalMmForSide(−1)。manager 只喂"下板吸盘口"这一个
//   世界锚点，上板锚点就靠这个间距 + 锚点朝向镜像得到 —— 尺寸与建模几何同源，不手算硬编码。
const HAND_MIRROR_MM = suckerLocalMmForSide(1, 0)[0] - suckerLocalMmForSide(-1, 0)[0];

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
  // ★ 井字棋符号色：O=蓝、X=绿；`Pend` = **预告态**（机器人先手时"已显示、待搬子"，
  //   半透明，不下落）—— 与后端 `stage` 文案「已显示，待搬子」一字对应。
  const matSym = {}, matSymPend = {};
  for (const [sym, hex] of Object.entries(TTT_SYM_HEX)) {
    matSym[sym] = mkMat(new THREE.MeshLambertMaterial({ color: hex }));
    matSymPend[sym] = mkMat(new THREE.MeshLambertMaterial({
      color: hex, transparent: true, opacity: 0.42 }));
  }
  // 顶面符号标记（圆环 / 十字）：不受光照影响，深色场景里也一眼看出是圆还是叉
  const matMark = mkMat(new THREE.MeshBasicMaterial({ color: HEX_MARK }));
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
  let symRingGeo = null;    // ★ 井字棋「圆」标记：圆环
  let symBarGeo = null;     // ★ 井字棋「叉」标记：一根横杠（两根交叉）
  let camBodyGeo = null;    // 摄像头机身
  let camLensGeo = null;    // 摄像头镜头
  let camGroup = null;      // C 区摄像头模型
  let camFlash = 0;         // 判色闪烁计时（秒）

  const aBlocks = new Map();   // name -> { mesh, edge }
  const bSockets = new Map();  // name -> LineSegments
  const bBlocks = new Map();   // name -> { mesh, edge }
  const trays = [];            // A/B 两个托盘盒（Group）
  const trayGeos = [];         // 托盘盒几何（每盒尺寸不同，须各自释放）
  let hand = null;             // 手上那块·**下板**（跟随末端，位置每帧由 manager 喂世界坐标）
  let handUpper = null;        // 手上那块·**上板**（双面新增；同一套锚点镜像定位）
  // 「手上那块」的朝向修正：绕自身 z 轴 −90°，把料的厚度方向对齐吸盘开口方向（−x）。
  // 建一次复用（每帧新建 Quaternion 会白白产生垃圾对象）。
  // ★ 料是 70×70 方料（x=z=70），绕 z 正负 90° 视觉等价；这里取 −90° 只是让"厚 14"这一维
  //   方向与下面的 −x 开口保持一致（改料尺寸为非方料时就不会错）。
  const Q_ROT_HAND = new THREE.Quaternion()
    .setFromAxisAngle(new THREE.Vector3(0, 0, 1), -Math.PI / 2);

  let slots = [];              // 后端返回的槽位表（已过滤：未示教/重叠不画）
  let planeZ = 0;              // 示教法兰高度(mm，基座系) —— **只用于体检显示**
  let planeH = 0;              // ★ 摆放面高度(mm，基座系)：底面坐这里，0 = 站台台面
  let sizeM = DEFAULT_BLOCK_MM.map((v) => v / 1000);
  let warned = [];             // 画不了的槽位原因（供界面如实提示）
  let lastKey = "";            // 状态签名：只在变化时改场景（每帧 mutate 会闪）
  // ★ 井字棋棋盘覆盖（2026-10-10）：null = 不干预（分拣照旧）；给出时按棋盘
  //   显隐 B 区方块、按 used 隐藏 A 区料。**只是"多显示一份已落子"**，
  //   不改任何分拣字段 —— 对局期间分拣不会跑，两者天然不打架（见方案 §6.3）。
  let tttOverride = null;      // { board:[...9], used:[...], holding:bool, pending:[cell] }
  // ★ 落子「坠落」动画：{ rec, t, baseY }（只在 step() 里推进，没调 step 就不动 → 位置即格子面）
  const falling = [];
  let firstUpdate = true;      // 刚装填后的第一次 update 不播动画（避免 9 块一起"砸"下来）

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
    if (handUpper) { if (handUpper.group.parent) handUpper.group.parent.remove(handUpper.group); handUpper = null; }
    if (camGroup) { if (camGroup.parent) camGroup.parent.remove(camGroup); camGroup = null; }
    trays.splice(0).forEach((t) => { if (t.parent) t.parent.remove(t); });
    if (blockGeo) { disposeGeo(blockGeo); blockGeo = null; }
    if (socketGeo) { disposeGeo(socketGeo); socketGeo = null; }
    if (symRingGeo) { disposeGeo(symRingGeo); symRingGeo = null; }
    if (symBarGeo) { disposeGeo(symBarGeo); symBarGeo = null; }
    if (camBodyGeo) { disposeGeo(camBodyGeo); camBodyGeo = null; }
    if (camLensGeo) { disposeGeo(camLensGeo); camLensGeo = null; }
    trayGeos.splice(0).forEach(disposeGeo);
    edgeGeos.splice(0).forEach(disposeGeo);
    falling.length = 0;         // 坠落动画跟着方块一起作废（否则会去写已释放的节点）
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
   * 井字棋**顶面符号标记**：O → 圆环、X → 两根交叉横杠；`sym=""` → 移除标记。
   * ★ 挂在方块 group 下（随方块一起显隐/坠落）；`rec.markSym` 缓存当前符号，避免每帧重建。
   */
  function setMark(rec, sym) {
    if (rec.markSym === sym) return;
    rec.markSym = sym;
    if (rec.mark) { rec.group.remove(rec.mark); rec.mark = null; }
    if (!sym || !symRingGeo || !symBarGeo) return;
    const g = new THREE.Group();
    g.name = "sym-mark";
    const dy = sizeM[2] / 2 + 0.0012;         // 贴在方块**顶面**之上一点点
    if (sym === "O") {
      const ring = new THREE.Mesh(symRingGeo, matMark);
      ring.rotation.x = -Math.PI / 2;         // RingGeometry 默认在 XY 面 → 放平到 XZ 面
      ring.position.y = dy;
      g.add(ring);
    } else if (sym === "X") {
      for (const rotY of [0, Math.PI / 2]) {
        const bar = new THREE.Mesh(symBarGeo, matMark);
        bar.rotation.y = rotY;
        bar.position.y = dy;
        g.add(bar);
      }
    }
    rec.group.add(g);
    rec.mark = g;
  }

  /**
   * 记一次「坠落」：把方块瞬移到格子上方 `FALL_H_MM`，之后由 `step(dt)` 落回 `baseY`。
   * ★ 只在 `step()` 里推进 —— 没调 `step`（自检/回归）时位置就是格子面高，不破坏"同高"断言。
   */
  function startFall(rec) {
    const baseY = rec.group.position.y;               // 静止高度 = 格子面
    rec.group.position.y = baseY + FALL_H_MM / 1000;  // 先抬到上方，再落下来
    falling.push({ rec, t: 0, baseY });
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
    // ★ 井字棋顶面标记：圆环（O）/ 十字的一根横杠（X）。尺寸随料尺寸走。
    symRingGeo = new THREE.RingGeometry(sizeM[0] * 0.19, sizeM[0] * 0.30, 28);
    symBarGeo = new THREE.BoxGeometry(sizeM[0] * 0.62, 0.0016, sizeM[0] * 0.10);

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
    falling.length = 0;
    firstUpdate = true;    // 刚装填 → 下一次 update 不播坠落动画（避免整盘一起"砸"）
    lastKey = "";
  }

  /** 手上那块·**下板**：跟随末端（位置由 manager 每帧喂世界坐标 mm）。 */
  function ensureHand() {
    if (hand) return hand;
    const b = mkBlock(matUnknown);
    b.group.visible = false;
    // ★ face = 哪一面在吸（lower=下板 −x 开口 / upper=上板 +x 开口）；
    //   供调试与回归读取（现场无真空反馈，"哪面在吸"是流程推断）。
    b.group.userData = { sortHand: true, face: "lower", inferred: false };
    root.add(b.group);
    hand = b;
    return hand;
  }

  /** 手上那块·**上板**（双面新增）：与下板对称，靠 step() 的镜像锚点定位。 */
  function ensureHandUpper() {
    if (handUpper) return handUpper;
    const b = mkBlock(matUnknown);
    b.group.visible = false;
    b.group.userData = { sortHand: true, face: "upper", inferred: false };
    root.add(b.group);
    handUpper = b;
    return handUpper;
  }

  /**
   * 井字棋棋盘覆盖：给出时 B 区按格子显隐方块、A 区按 used 隐藏料。
   * @param {{board?:Array, used?:Array, holding?:boolean, pending?:Array<number>}|null} o
   *        传 null 清除覆盖。
   * ★ 只改可视化，不动分拣逻辑；重置 lastKey 让下一次 update() 立刻重画。
   * ★ `board` 值 "O"/"X" 直接决定棋子颜色与顶面符号（蓝圆 / 绿叉）；
   *   `pending` = 机器人先手"已显示、待搬子"的格子下标（0–8）→ 半透明、不坠落。
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
    // ★ 井字棋「预告态」：机器人先手时后端**提前把子写进棋盘**（stage="已显示，待搬子"），
    //   此刻**尚未真搬** —— 这些格子渲染成半透明、不播坠落，别让人误以为"已经放好了"。
    const tttPending = (tttOn && Array.isArray(tttOverride.pending))
      ? new Set(tttOverride.pending.map((c) => "B" + (Number(c) + 1))) : null;
    const placed = tttOn ? 0 : (Array.isArray(s.placement) ? s.placement : [])
      .reduce((n, c) => n + (Number(c.filled) || 0), 0);
    const holding = tttOn ? !!tttOverride.holding : !!s.holding;
    const suspect = tttOn ? false : !!s.suspect_pick;
    const piece = tttOn ? 0 : (Number(s.piece) || 0);
    const colorName = SORT_COLOR_HEX[s.color] ? s.color : "";
    const simColor = !!s.sim_color;

    // ★ 双面两块料（2026-10-10）：后端会给 `hold_lower` / `hold_upper`（各为 null 或 {color, ok}）。
    //   · 两个字段任一存在 → 按双面处理（两块可同时可见）；
    //   · 只有旧的 `holding` → 退回"下板单件"，行为与以前**完全一致**（零回归）；
    //   · 井字棋覆盖期间沿用它的 holding（单件·下板）。
    const dual = !tttOn && ("hold_lower" in s || "hold_upper" in s);
    // "哪一面在吸"是**流程推断**（现场无真空反馈）→ 如实带到 userData，不编"确实吸住了"。
    const inferFlag = !!s.hold_inferred;
    let holdL = null, holdU = null;
    if (tttOn) {
      if (tttOverride.holding) holdL = { color: "", ok: true };
    } else if (dual) {
      holdL = s.hold_lower || null;
      holdU = s.hold_upper || null;
    } else if (holding) {
      holdL = { color: colorName, ok: true };
    }
    const heldCount = (holdL ? 1 : 0) + (holdU ? 1 : 0);
    const holdKey = [holdL ? (holdL.color || "-") + (holdL.ok === false ? "!" : "") : "_",
                     holdU ? (holdU.color || "-") + (holdU.ok === false ? "!" : "") : "_",
                     inferFlag ? 1 : 0].join(",");
    const key = [placed, holding, suspect, piece, colorName, simColor,
                 s.stage, s.running, s.dry_run, holdKey,
                 tttB ? tttB.map((v) => v || ".").join("") : "",
                 tttUsed ? tttUsed.join(",") : "",
                 tttPending ? [...tttPending].join(",") : ""].join("|");
    if (key === lastKey) return;
    lastKey = key;

    // A 区：已放走的 + （在手上的 / 疑似拿走的）都消失
    const goneA = new Set();
    for (let i = 1; i <= placed; i++) goneA.add("A" + i);
    // 在手上的料按 A 区顺序占掉接下来 heldCount 格（单件时 heldCount=1 → 与旧行为一致）
    for (let k = 1; k <= heldCount; k++) {
      if (placed + k <= 9) goneA.add("A" + (placed + k));
    }
    // ★ 井字棋：已取用的 A 格同样消失（棋子已被搬到 B 区）
    if (tttUsed) for (const nm of tttUsed) goneA.add(String(nm).trim().toUpperCase());
    // 失败停止（疑似没取到）：该格**画回来**并标红 —— 不带走、不显示夹持
    const suspectCell = (suspect && piece) ? ("A" + piece) : "";
    aBlocks.forEach((b, name) => {
      const idx = Number(String(name).slice(1));
      const taken = goneA.has(name);
      const isSuspect = name === suspectCell;
      const wasVisible = b.group.visible;
      b.group.visible = !taken || isSuspect;
      b.group.userData.suspect = isSuspect;
      if (tttOn) {
        // ★ 井字棋：A 区料按**来源符号**上色（A1–A5 = 圆蓝 / A6–A9 = 叉绿）+ 顶面标记。
        //   这就是用户说的"不是全部白色"—— 一眼看出哪几块是圆、哪几块是叉。
        const sym = tttSymForASlot(name);
        b.group.userData.sym = sym;
        setMark(b, sym);
        b.mesh.material = matSym[sym] || matBlack;
      } else {
        const hot = isSuspect || (!!s.running && !heldCount && idx === piece);
        b.mesh.material = isSuspect ? matSuspect : matBlack;
        // 取料中的那格高亮（emissive 让"正在取这一格"一眼可见）
        try {
          const m = b.mesh.material;
          if (m && m.emissive) m.emissive.setHex(hot && !isSuspect ? 0x2b3a52 : 0x000000);
        } catch (e) { /* 忽略 */ }
      }
      // ★ 井字棋：被"放回原位"时也走一次坠落（与落子同一套手感）
      if (tttOn && !wasVisible && b.group.visible && !firstUpdate) startFall(b);
    });

    // B 区：已占位的槽显示实心彩色块，其余留空槽线框
    // ★ 井字棋覆盖期间：按**棋盘符号**上色（O=蓝圆 / X=绿叉）；预告态半透明且不坠落
    const usedSlot = new Map();      // slot -> 颜色名（分拣模式）
    const symSlot = new Map();       // ★ 井字棋：slot -> "O"|"X"
    if (!tttOn) {
      (Array.isArray(s.placement) ? s.placement : []).forEach((c) => {
        (c.used || []).forEach((nm) => usedSlot.set(nm, c.color));
      });
    } else if (tttB) {
      for (let i = 0; i < 9; i++) if (tttB[i]) symSlot.set("B" + (i + 1), tttB[i]);
    }
    bBlocks.forEach((blk, name) => {
      const wasVisible = blk.group.visible;
      if (tttOn) {
        const sym = symSlot.get(name) || "";
        const pend = !!(sym && tttPending && tttPending.has(name));
        const mkey = sym + (pend ? "!" : "");
        if (blk.group.userData.tttKey !== mkey) {
          blk.group.userData.tttKey = mkey;
          blk.group.userData.sym = sym;
          blk.group.userData.pending = pend;
          setMark(blk, sym);
          blk.mesh.material = sym ? (pend ? matSymPend[sym] : matSym[sym]) : matUnknown;
        }
        blk.group.visible = !!sym;
        // 预告态（已显示、待搬子）**不播坠落**：还没真搬，动画不能"编故事"
        if (!wasVisible && sym && !pend && !firstUpdate) startFall(blk);
        return;
      }
      const cn = usedSlot.get(name);
      const mat = cn && matByColor[cn] ? matByColor[cn] : matUnknown;
      if (blk.group.userData.colorName !== (cn || "")) {
        blk.group.userData.colorName = cn || "";
        blk.mesh.material = mat;
      }
      blk.group.visible = !!cn;
    });

    // 手上料：空跑（占位色）或未判色 → 未知灰，**绝不编一个颜色**出来。
    // ★ 双面时上/下两块**各取各色**（两件可能不同色、也可能一件无效色）；
    //   只有旧 holding 时只画下板那一块 —— 与以前一致。
    const matForHeld = (rec) => {
      const cn = (rec && SORT_COLOR_HEX[rec.color]) ? rec.color : "";
      return (!simColor && cn) ? matByColor[cn] : matUnknown;
    };
    const hb = ensureHand();
    hb.group.visible = !!holdL;
    hb.group.userData.inferred = inferFlag;    // 如实标注：哪面在吸是**流程推断**
    if (holdL) hb.mesh.material = matForHeld(holdL);
    const hu = ensureHandUpper();
    hu.group.visible = !!holdU;
    hu.group.userData.inferred = inferFlag;
    if (holdU) hu.mesh.material = matForHeld(holdU);

    // 摄像头判色闪烁
    if (String(s.stage || "").includes("判色")) camFlash = 0.9;

    firstUpdate = false;   // 只有"装填后的第一次"不播坠落动画
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
    // ★ 落子「坠落」：把方块从格子上方缓动落到格子面（easeOutCubic，末态**严格**回格子面）。
    //   纯视觉，不参与任何安全/碰撞判定；只在 step() 里推进（没调 step 就不动）。
    for (let i = falling.length - 1; i >= 0; i--) {
      const f = falling[i];
      f.t += dt;
      const p = Math.min(1, f.t / FALL_DUR);
      const e = 1 - Math.pow(1 - p, 3);
      f.rec.group.position.y = f.baseY + (FALL_H_MM / 1000) * (1 - e);
      if (p >= 1) { f.rec.group.position.y = f.baseY; falling.splice(i, 1); }
    }
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
    /**
     * 把一块"手上料"摆到吸盘口上。
     * @param {object} o 目标方块 {group}
     * @param {boolean} upper false = 下板(−x 开口) · true = 上板(+x 开口)
     * ★ 锚点只有**下板那一个**（manager 喂）；上板锚点沿工具局部 +x 平移 HAND_MIRROR_MM
     *   镜像出来（口径见文件头/常量注释，尺寸与建模几何同源）。
     */
    const placeAt = (o, upper) => {
      if (anchorMm) {
        // 吸盘口锚点：下板料心 = 锚点 + (−x 开口方向)·半厚（现有位置，保持不变）。
        const q = new THREE.Quaternion(anchorMm.q[0], anchorMm.q[1],
                                       anchorMm.q[2], anchorMm.q[3]);
        const out = new THREE.Vector3(-1, 0, 0).applyQuaternion(q);   // 锚点 −x 世界方向（下板开口）
        const halfMm = sizeM[2] * 1000 / 2;                           // 半料厚(mm)
        // 上板：吸盘口 = 锚点沿工具局部 +x 平移 HAND_MIRROR_MM → 料心 = 锚点 − out·(间距+半厚)
        const sgn = upper ? -1 : 1;
        const offMm = upper ? (HAND_MIRROR_MM + halfMm) : halfMm;
        o.group.position.set(
          (anchorMm.x + sgn * out.x * offMm) / 1000,
          (anchorMm.y + sgn * out.y * offMm) / 1000,
          (anchorMm.z + sgn * out.z * offMm) / 1000,
        );
        // 料默认是"平躺"的：BoxGeometry(x=长, y=厚, z=宽)。让**厚度方向（局部 y）对齐吸盘
        // 开口（局部 x）** → 绕**自身 z 轴**转 −90°：
        //   (x,y,z) → (y, −x, z)：原 y(14 厚) → x 方向 ✓，原 x(70) → y ✓，z(70) 不变 ✓
        // ⚠ 别转 y 轴：那是把长度转到 x，"料会竖着插进吸盘"（第一版就写错了）。
        o.group.quaternion.copy(q).multiply(Q_ROT_HAND);
      } else if (tcpMm) {
        // 兜底（锚点还没建好时）：退回法兰中心附近，保证"至少跟着动、不消失"。
        // 此刻没有姿态信息，上下两块只能朝相反侧各偏一点，避免完全重叠。
        const dy = (upper ? 1 : -1) * (sizeM[2] / 2 + 0.004);
        o.group.position.set(tcpMm.x / 1000, tcpMm.y / 1000 + dy, tcpMm.z / 1000);
        o.group.quaternion.identity();
      }
    };
    if (hand && hand.group.visible) placeAt(hand, false);
    if (handUpper && handUpper.group.visible) placeAt(handUpper, true);
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
      handUpper: !!handUpper,
      handMirrorMm: HAND_MIRROR_MM,
      warned: warned.slice(), slots: slots.length,
    };
  }

  return { root, setSlots, update, setTtt, step, setVisible, dispose, info };
}
