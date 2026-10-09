// =====================================================================
// 安全围栏引擎：按配置构建（矩形 / 四点 / 圆形 · 多区域）+ 每帧评估。
//
// 设计要点
// 1) 几何：任何形状都归一化为"水平多边形"，再统一做面板 + 线框 + 角柱。
// 2) 间隙：多边形用"内法线半空间 vs AABB 四角"求最小间隙；圆形用精确式
//    R − (AABB 距圆心最远点距离)。二者都是**保守估计**（宁可报小不报大）。
// 3) 分级：ratio = clearance / basis，basis 由 thresholds.basis 决定
//    （halfwidth=中心到最近边的距离 / fixed=固定毫米）。
// 4) 热重建：applyConfig() 全量重建（区域最多 8 个，开销可忽略），
//    避免"改一个数字要刷新页面"。
// =====================================================================
import * as THREE from "three";

const SEGMENTS = 36;             // 圆形离散段数（渲染 + 线框共用）
const GROUND_PAD_Y = 0.004;      // 地面报警垫高度：略高于地板(-0.002)与网格(0)，避免闪面
export const STATE_RANK = { safe: 0, warn: 1, danger: 2, hit: 3 };

/**
 * 四态兜底色/透明度。
 * ★ 关键：即使配置里缺 colors/opacity（例如后端是旧版本、或旧配置文件还没补字段），
 *   也必须按"状态"给出正确颜色 —— 否则会全部回退成安全色(绿)，
 *   出现"接近不变黄、碰撞闪烁还是绿色"这种问题。
 */
export const FALLBACK_COLORS = {
  safe: "#2ecc71", warn: "#f2c500", danger: "#e5484d", hit: "#ff2020",
};
export const FALLBACK_OPACITY = { safe: 0.10, warn: 0.22, danger: 0.34, hit: 0.50 };

/** 取某状态的颜色（配置优先，缺省按状态兜底）。 */
export function colorOf(state, colors) {
  return (colors && colors[state]) || FALLBACK_COLORS[state] || FALLBACK_COLORS.safe;
}
/** 取某状态的透明度（配置优先，缺省按状态兜底）。 */
export function opacityOf(state, opacity) {
  const v = opacity && opacity[state];
  return Number.isFinite(Number(v)) ? Number(v) : (FALLBACK_OPACITY[state] ?? 0.2);
}

function num(v, d) {
  const n = Number(v);
  return Number.isFinite(n) ? n : d;
}

/** 区域水平多边形（世界 XZ 平面，单位 m），按 逆时针/顺时针 都行。 */
export function zonePolygon(z) {
  const cx = num(z.center && z.center.x, 0);
  const cz = num(z.center && z.center.z, 0);
  if (z.shape === "circle") {
    const R = num(z.radius, 0.9);
    const pts = [];
    for (let i = 0; i < SEGMENTS; i++) {
      const a = (i / SEGMENTS) * Math.PI * 2;
      pts.push([cx + R * Math.cos(a), cz + R * Math.sin(a)]);
    }
    return pts;
  }
  if (z.shape === "quad") {
    const cs = (z.corners || []).map((p) => [num(p && p[0], 0), num(p && p[1], 0)]);
    if (cs.length === 4) return cs;
  }
  const hx = num(z.half && z.half.x, 0.82);
  const hz = num(z.half && z.half.z, 0.82);
  return [
    [cx - hx, cz - hz],
    [cx + hx, cz - hz],
    [cx + hx, cz + hz],
    [cx - hx, cz + hz],
  ];
}

/**
 * 多边形 → 内法线半空间列表。
 * 内法线取"指向质心"的那个候选，因此不依赖顶点绕向。
 */
export function polygonHalfspaces(pts) {
  if (!pts || pts.length < 3) return [];
  let cx = 0, cz = 0;
  for (const p of pts) { cx += p[0]; cz += p[1]; }
  cx /= pts.length; cz /= pts.length;

  const out = [];
  for (let i = 0; i < pts.length; i++) {
    const a = pts[i];
    const b = pts[(i + 1) % pts.length];
    let dx = b[0] - a[0];
    let dz = b[1] - a[1];
    const L = Math.hypot(dx, dz);
    if (L < 1e-9) continue;
    dx /= L; dz /= L;
    let nx = -dz, nz = dx;
    if (nx * (cx - a[0]) + nz * (cz - a[1]) < 0) { nx = -nx; nz = -nz; }
    out.push({ nx, nz, ax: a[0], az: a[1], dx, dz, len: L });
  }
  return out;
}

/** AABB（水平投影四角）到多边形的最小间隙(m)。负值 = 已越界。 */
export function clearanceToPolygon(box, hs) {
  const xs = [box.min.x, box.max.x];
  const zs = [box.min.z, box.max.z];
  let best = Infinity;
  for (const e of hs) {
    let d = Infinity;
    for (const x of xs) {
      for (const z of zs) {
        const v = e.nx * (x - e.ax) + e.nz * (z - e.az);
        if (v < d) d = v;
      }
    }
    if (d < best) best = d;
  }
  return best === Infinity ? 0 : best;
}

/** 圆形区域精确间隙：R − AABB 上距圆心最远点的距离。 */
export function clearanceToCircle(box, cx, cz, R) {
  const px = Math.abs(box.min.x - cx) > Math.abs(box.max.x - cx) ? box.min.x : box.max.x;
  const pz = Math.abs(box.min.z - cz) > Math.abs(box.max.z - cz) ? box.min.z : box.max.z;
  return R - Math.hypot(px - cx, pz - cz);
}

/** 分级基准距离(m)：halfwidth=中心到最近边 / fixed=固定毫米。 */
export function basisOf(z) {
  const t = z.thresholds || {};
  if (t.basis === "fixed") return Math.max(1e-6, num(t.fixed_mm, 300) / 1000);
  if (z.shape === "circle") return Math.max(1e-6, num(z.radius, 0.9));
  const cx = num(z.center && z.center.x, 0);
  const cz = num(z.center && z.center.z, 0);
  let m = Infinity;
  for (const e of polygonHalfspaces(zonePolygon(z))) {
    const d = e.nx * (cx - e.ax) + e.nz * (cz - e.az);
    if (d < m) m = d;
  }
  if (!Number.isFinite(m) || m <= 1e-6) {
    m = Math.min(num(z.half && z.half.x, 0.82), num(z.half && z.half.z, 0.82));
  }
  return Math.max(1e-6, m);
}

/**
 * 间隙 + 阈值 → 状态。
 * ratio <= 0 → hit；< danger → danger；< warn → warn；否则 safe。
 */
export function classify(clearance, basis, th) {
  const warn = num(th && th.warn, 0.3);
  const danger = num(th && th.danger, 0.1);
  const b = basis > 1e-6 ? basis : 1;
  const ratio = clearance / b;
  let state = "safe";
  if (ratio <= 0) state = "hit";
  else if (ratio < danger) state = "danger";
  else if (ratio < warn) state = "warn";
  return { state, ratio, clearance };
}

/**
 * 地面碰撞分级（纯函数，便于单元测试）。与围栏面板同构的四级：
 *   min_y <= hit_mm    → hit（碰撞地面，红闪）
 *   min_y <= danger_mm → danger（危险，红）
 *   min_y <= warn_mm   → warn（接近地面，黄）
 *   否则               → safe（安全，绿，淡）
 * @param {number} minY 机器人最低点**相对立足面**的高度(m)（机器人站站台上时立足面=台面）
 * @param {object} g 地面配置 { warn_mm, danger_mm, hit_mm }
 */
export function classifyGround(minY, g) {
  const warn = num(g && g.warn_mm, 100) / 1000;
  const danger = num(g && g.danger_mm, 50) / 1000;
  const hit = num(g && g.hit_mm, 20) / 1000;
  if (minY <= hit) return "hit";
  if (minY <= danger) return "danger";
  if (minY <= warn) return "warn";
  return "safe";
}

/**
 * 地面报警垫的平面形状（世界 XZ 的多边形点列，单位 m）。
 * ★ 跟随"主工作区"（第一个启用区域）—— 也就是**四个角柱围成的那块地面**
 *   （矩形/正方形/任意四边形/圆形都支持），而不是一个独立的圆盘。
 */
export function groundFootprint(cfg) {
  const zones = (cfg && cfg.zones) || [];
  const z = zones.find((zz) => zz && zz.enabled !== false) || zones[0];
  if (z) return zonePolygon(z);
  // 没有任何区域时的兜底：给一块与默认主工作区同样大小的正方形
  // （正常情况下 normalizeSafetyConfig 至少会补出一个 z1，走不到这里）
  const h = 0.82;
  return [[-h, -h], [h, -h], [h, h], [-h, h]];
}

/**
 * 水平多边形（世界 XZ，单位 m）→ 位于高度 y 的平面几何。
 *
 * 采用「质心扇形」三角化：对矩形/正方形/任意四边形/圆形离散多边形都成立
 * （比从某个顶点扇形拆分更稳）。面法线统一取 +Y，配合材质的 DoubleSide，
 * 顶点绕向不影响渲染。
 */
export function polygonGeometry(pts, y = 0) {
  const geo = new THREE.BufferGeometry();
  const n = (pts || []).length;
  if (n < 3) return geo;
  let cx = 0, cz = 0;
  for (const p of pts) { cx += p[0]; cz += p[1]; }
  cx /= n; cz /= n;

  const pos = [];
  for (let i = 0; i < n; i++) {
    const a = pts[i], b = pts[(i + 1) % n];
    pos.push(cx, y, cz, a[0], y, a[1], b[0], y, b[1]);
  }
  geo.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
  const nor = new Float32Array(pos.length);
  for (let i = 1; i < nor.length; i += 3) nor[i] = 1;   // 全 (0, 1, 0)
  geo.setAttribute("normal", new THREE.BufferAttribute(nor, 3));
  geo.computeBoundingBox();
  return geo;
}

/** 单个区域的完整求值（纯函数，便于单元测试）。 */
export function evaluateZone(z, box) {
  let clearance;
  if (z.shape === "circle") {
    clearance = clearanceToCircle(
      box, num(z.center && z.center.x, 0), num(z.center && z.center.z, 0), num(z.radius, 0.9)
    );
  } else {
    clearance = clearanceToPolygon(box, polygonHalfspaces(zonePolygon(z)));
  }
  const r = classify(clearance, basisOf(z), z.thresholds);
  return { id: z.id || "", name: z.name || "", ...r };
}

/**
 * 逐边（= 逐面墙）间隙评估（纯函数，便于单元测试）。
 *
 * 围栏的每一面墙 = 多边形的一条边。用该边的**内法线**去量机器人 AABB 四个角，
 * 取最小有向距离，就是"到这一面墙还剩多少余量"。于是可以
 * **只让正在接近的那一面墙变黄/变红**，而不是四面墙一起变。
 *
 * 返回数组与 `polygonHalfspaces(zonePolygon(z))` **顺序一一对应**（渲染时按同序建墙），
 * 每项为 `classify()` 的结果外加 `basis`。
 */
export function evaluateEdges(z, box) {
  const hs = polygonHalfspaces(zonePolygon(z));
  const t = z.thresholds || {};
  const fixed = t.basis === "fixed" ? Math.max(1e-6, num(t.fixed_mm, 300) / 1000) : null;
  const cx = num(z.center && z.center.x, 0);
  const cz = num(z.center && z.center.z, 0);
  const xs = [box.min.x, box.max.x];
  const zs = [box.min.z, box.max.z];

  return hs.map((e) => {
    let d = Infinity;
    for (const x of xs) {
      for (const zz of zs) {
        const v = e.nx * (x - e.ax) + e.nz * (zz - e.az);
        if (v < d) d = v;
      }
    }
    if (!Number.isFinite(d)) d = 0;
    // 这面墙的基准：区域中心到该墙的距离（halfwidth 口径）；fixed 口径则四面相同
    const basis = fixed != null ? fixed : Math.max(1e-6, e.nx * (cx - e.ax) + e.nz * (cz - e.az));
    return { ...classify(d, basis, t), basis };
  });
}

const _col = new THREE.Color();

export class SafetyFence {
  /** @param {THREE.Object3D} parent 挂到哪个节点（一般是 scene） */
  constructor(parent) {
    this.parent = parent || null;
    this.group = new THREE.Group();
    this.group.name = "safety-fence";
    if (this.parent) this.parent.add(this.group);

    this.cfg = null;
    // 每个区域: { cfg, group, postMat, halfspaces, basis,
    //             walls: [{ hs, panelMat, lineMat, colors, opacity, state }] }
    // ★ walls 一面墙一项、各自独立材质 —— 只让正在接近的那面墙变色。
    this.zones = [];
    this.box = new THREE.Box3();
    this.groundBox = new THREE.Box3();   // 地面检测专用（J1 以上子树）
    this.helper = null;              // 包围盒线框（碰撞预测）
    this.helperTarget = null;

    // 地面报警垫：铺满"四个角柱围成的那块地面"（区域多边形），随地面状态变色
    this.groundMat = null;
    this.groundMesh = null;
    this.groundEdge = null;          // 垫子四周的状态描边（LineLoop）
    this.groundEdgeMat = null;
    // 垫子是否改铺到抬高的水平面（机器人站台的台面）；null = 默认铺地坪
    this.padPlane = null;
  }

  // ---------------- 构建 ----------------
  applyConfig(cfg) {
    this.cfg = cfg || null;
    this._clear();
    if (!cfg) { this.group.visible = false; return; }
    (cfg.zones || []).forEach((z) => {
      if (z.enabled === false) return;
      this._buildZone(z);
    });
    this.group.visible = cfg.enabled !== false;
    this._buildGround();
    this.setOverlay(!!(cfg.overlay && cfg.overlay.bbox));
  }

  _clear() {
    this.zones.forEach((z) => {
      z.group.traverse((o) => {
        if (o.geometry) o.geometry.dispose();
      });
      (z.walls || []).forEach((w) => {
        if (w.panelMat) w.panelMat.dispose();
        if (w.lineMat) w.lineMat.dispose();
      });
      if (z.postMat) z.postMat.dispose();
      (z.postMats || []).forEach((m) => m.dispose());
      this.group.remove(z.group);
    });
    this.zones = [];
    this._disposeGround();
  }

  /**
   * 地面报警垫：**铺满"四个角柱围成的那块地面"** —— 即区域多边形
   * （矩形/正方形/任意四边形/圆形都跟随），随地面碰撞状态着色。
   *
   * ★ 不再是一个固定半径的圆盘：用户要的就是"四个柱子连起来的那块地"。
   */
  _buildGround() {
    const g = this.cfg && this.cfg.ground;
    if (!g || g.enabled === false) return;
    // 垫子铺哪儿：默认=区域多边形(房间地坪)；设了 padPlane=站台台面（方形，跟随台面尺寸）
    const padY = this.padPlane ? this.padPlane.y : GROUND_PAD_Y;
    const fp = this.padPlane
      ? (() => {
          const h = this.padPlane.size / 2;
          return [[-h, -h], [h, -h], [h, h], [-h, h]];
        })()
      : groundFootprint(this.cfg);
    const col = colorOf("safe", g.colors);
    const mat = new THREE.MeshStandardMaterial({
      color: new THREE.Color(col),
      emissive: new THREE.Color(col),
      emissiveIntensity: 0.3,
      transparent: true,
      opacity: opacityOf("safe", g.opacity),
      depthWrite: false,
      side: THREE.DoubleSide,
      metalness: 0.0,
      roughness: 0.4,
    });
    const geo = polygonGeometry(fp, 0);
    const mesh = new THREE.Mesh(geo, mat);
    mesh.name = "ground-pad";
    mesh.position.y = padY;             // 几何已平铺在 XZ 平面，这里只抬高度
    mesh.renderOrder = 1;               // 在地板/网格之后绘制，避免被压住
    mesh.visible = this.group.visible;
    this.group.add(mesh);
    this.groundMat = mat;
    this.groundMesh = mesh;

    // ---- 状态描边：垫子四周一圈高亮边线，与垫面同一套四级变色 ----
    // 比半透明的垫面醒目得多（远看先看到描边变色），hit 时随 cfg.blink 一起闪。
    const pts = fp;
    const eGeo = new THREE.BufferGeometry().setFromPoints(
      pts.map((p) => new THREE.Vector3(p[0], 0, p[1]))
    );
    const edgeMat = new THREE.LineBasicMaterial({
      color: new THREE.Color(col), transparent: true, opacity: 0.85, depthWrite: false,
    });
    const loop = new THREE.LineLoop(eGeo, edgeMat);
    loop.name = "ground-pad-edge";
    loop.position.y = padY + 0.001;
    loop.renderOrder = 2;
    loop.visible = this.group.visible;
    this.group.add(loop);
    this.groundEdge = loop;
    this.groundEdgeMat = edgeMat;
  }

  /**
   * 把地面报警垫改铺到一个"抬高的水平面"上（例如机器人站台的台面）。
   *
   * 为什么需要：机器人站到站台上以后，它可能撞到的"地面"就是**台面**。
   * 若垫子仍铺在 y≈0 的房间地坪上，就会被站台本体整块盖住 ——
   * 只剩四周一条窄边露出来，变色报警几乎看不见。
   *
   * ★ 传 null 恢复默认行为（铺在区域多边形上，y = GROUND_PAD_Y）。
   * ★ 该设置挂在实例上（不写进 cfg），所以之后任何一次 `applyConfig()`
   *   重建垫子时都会自动沿用，不会被打回地坪。
   *
   * @param {{y:number, size:number}|null} p 台面高度(m) 与台面边长(m)
   */
  setPadPlane(p) {
    this.padPlane = p && Number.isFinite(p.y) && Number.isFinite(p.size)
      ? { y: p.y, size: p.size }
      : null;
    if (this.cfg) this._rebuildGround();
  }

  /** 只重建地面报警垫（不动区域/墙/柱子）。 */
  _rebuildGround() {
    this._disposeGround();
    this._buildGround();
    if (this.groundMesh) this.groundMesh.visible = this.group.visible;
    if (this.groundEdge) this.groundEdge.visible = this.group.visible;
  }

  _disposeGround() {
    if (this.groundMesh) {
      this.groundMesh.geometry.dispose();
      this.group.remove(this.groundMesh);
      this.groundMesh = null;
    }
    if (this.groundMat) {
      this.groundMat.dispose();
      this.groundMat = null;
    }
    if (this.groundEdge) {
      this.groundEdge.geometry.dispose();
      this.group.remove(this.groundEdge);
      this.groundEdge = null;
    }
    if (this.groundEdgeMat) {
      this.groundEdgeMat.dispose();
      this.groundEdgeMat = null;
    }
  }

  _buildZone(z) {
    const g = new THREE.Group();
    g.name = "zone-" + (z.id || "?");
    this.group.add(g);

    const pts = zonePolygon(z);
    const hs = polygonHalfspaces(pts);
    const h = num(z.height, 1.2);
    const cx = num(z.center && z.center.x, 0);
    const cz = num(z.center && z.center.z, 0);
    const showWalls = z.walls !== false && this.cfg.walls !== false;
    const y0 = 0.002, y1 = h;
    const safeCol = colorOf("safe", z.colors);
    const safeOp = opacityOf("safe", z.opacity);

    // ---- 一面墙一份独立材质 + 自己的线框 ----
    // ★ 只让"正在接近的那一面墙"变黄/变红，不再四面墙共用一份材质一起变色。
    //   墙的顺序与 evaluateEdges() 的返回顺序严格一致（都来自 hs）。
    const walls = [];
    hs.forEach((e) => {
      const panelMat = new THREE.MeshStandardMaterial({
        color: new THREE.Color(safeCol),
        emissive: new THREE.Color(safeCol),
        emissiveIntensity: 0.35,
        transparent: true,
        opacity: safeOp,
        depthWrite: false,
        side: THREE.DoubleSide,
        metalness: 0.0,
        roughness: 0.12,      // 低粗糙度：借环境贴图呈现玻璃质感
      });
      const lineMat = new THREE.LineBasicMaterial({
        color: new THREE.Color(safeCol), transparent: true, opacity: 0.5,
      });

      const ex = e.ax + e.dx * e.len, ez = e.az + e.dz * e.len;

      // 面板：PlaneGeometry 局部 +X 对齐边方向，法线自动垂直该边
      // （圆形也走这条路径 —— 36 段离散多边形 ≈ 圆柱面，且能与逐墙着色对齐）
      if (showWalls) {
        const m = new THREE.Mesh(new THREE.PlaneGeometry(e.len, h), panelMat);
        m.name = "wall";
        m.rotation.y = Math.atan2(-e.dz, e.dx);
        m.position.set((e.ax + ex) / 2, h / 2, (e.az + ez) / 2);
        g.add(m);
      }

      // 这面墙自己的线框：底边 + 顶边 + 终点立棱（立棱归本墙 → 每条棱只画一次）
      const pos = [
        e.ax, y0, e.az, ex, y0, ez,     // 底边
        e.ax, y1, e.az, ex, y1, ez,     // 顶边
        ex, y0, ez, ex, y1, ez,         // 立棱
      ];
      const lg = new THREE.BufferGeometry();
      lg.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
      g.add(new THREE.LineSegments(lg, lineMat));

      walls.push({
        hs: e, panelMat, lineMat, state: "safe",
        colors: z.colors, opacity: z.opacity,
      });
    });

    // ---- 角柱：工业防护柱（底板+螺栓 / 黑色警示柱脚 / 反光带 / 金属顶盖） ----
    // 颜色仍取 posts.color（默认安全黄），骨架细节用中性灰黑，不参与报警变色。
    const p = z.posts || {};
    let postMat = null;
    let postMats = [];
    if (p.enabled !== false) {
      const s = num(p.size, 0.1);
      postMat = new THREE.MeshStandardMaterial({
        color: new THREE.Color(p.color || "#2fd6f0"),
        metalness: 0.4, roughness: 0.35,
        emissive: new THREE.Color("#2fd6f0"),
        emissiveIntensity: 0.12,
        transparent: true,
        opacity: 0.8,
      });
      const baseMat = new THREE.MeshStandardMaterial({ color: 0x1a2a3a, metalness: 0.55, roughness: 0.45 });
      const darkMat = new THREE.MeshStandardMaterial({ color: 0x0d1520, metalness: 0.3, roughness: 0.62 });
      const capMat = new THREE.MeshStandardMaterial({ color: 0x3a5a7a, metalness: 0.7, roughness: 0.3 });
      const refMat = new THREE.MeshStandardMaterial({
        color: 0xe8f0fc, emissive: 0x2fd6f0, emissiveIntensity: 0.25,
        metalness: 0.2, roughness: 0.35,
      });
      postMats = [postMat, baseMat, darkMat, capMat, refMat];

      let anchors = pts;
      if (z.shape === "circle") {
        const R = num(z.radius, 0.9);
        anchors = [0, 1, 2, 3].map((k) => {
          const a = Math.PI / 4 + (k * Math.PI) / 2;
          return [cx + R * Math.cos(a), cz + R * Math.sin(a)];
        });
      }
      const baseS = s * 1.9;                 // 底板
      const capS = s * 1.28;                 // 顶盖
      const boltS = baseS / 2 - 0.014;
      anchors.forEach((a) => {
        const ax = a[0], az = a[1];
        const add = (w, hh, d, mat, y, name) => {
          const m = new THREE.Mesh(new THREE.BoxGeometry(w, hh, d), mat);
          m.position.set(ax, y, az);
          m.name = name;
          g.add(m);
        };
        add(baseS, 0.028, baseS, baseMat, 0.014, "post-base");            // 底板
        for (const [bx, bz] of [[-boltS, -boltS], [boltS, -boltS], [-boltS, boltS], [boltS, boltS]]) {
          const bolt = new THREE.Mesh(new THREE.BoxGeometry(0.018, 0.014, 0.018), capMat);
          bolt.position.set(ax + bx, 0.034, az + bz);
          bolt.name = "post-bolt";
          g.add(bolt);
        }
        add(s * 1.06, 0.14, s * 1.06, darkMat, 0.11, "post-warnband");    // 黑色警示柱脚
        add(s, h - 0.18, s, postMat, 0.18 + (h - 0.18) / 2, "post-body"); // 柱身
        add(s * 1.05, 0.07, s * 1.05, refMat, h * 0.66, "post-reflect");  // 反光带
        add(capS, 0.04, capS, capMat, h + 0.02, "post-cap");              // 金属顶盖
      });
    }

    this.zones.push({
      cfg: z, group: g, walls, postMat, postMats, halfspaces: hs, basis: basisOf(z),
    });
  }

  // ---------------- 包围盒线框（碰撞预测） ----------------
  setOverlay(on, target) {
    if (target) this.helperTarget = target;
    if (on && !this.helper) {
      this.helper = new THREE.Box3Helper(this.box, new THREE.Color(0x2ecc71));
      this.helper.material.depthTest = false;
      this.helper.material.transparent = true;
      this.helper.material.opacity = 0.8;
      this.group.add(this.helper);
    }
    if (this.helper) this.helper.visible = !!on;
  }

  // ---------------- 每帧评估 ----------------
  /**
   * 每帧评估。
   * @param {THREE.Object3D|null} root 当前生效的机器人根节点（区域碰撞用，含底座）
   * @param {THREE.Object3D|null} groundRoot 地面碰撞检测对象（J1 以上子树，排除贴地底座）
   * @param {number} tSec 秒（用于闪烁相位）
   * @param {number} refFloorY 立足面世界高度(m)：机器人站站台上时传台面高度，
   *   地面净空按"相对台面"算。缺省 0（直接站在地面上）。
   */
  update(root, groundRoot, tSec, refFloorY = 0) {
    const out = {
      state: "safe", ratio: 1, clearance: 0, zoneId: "", zoneName: "", zones: [],
    };
    const g = this.cfg && this.cfg.ground;
    const gOn = !!(g && g.enabled !== false && this.cfg.enabled !== false);

    // ---- 地面：先算 + 先着色 ----
    // 放在所有早退分支之前，保证报警垫**每帧都会被着色**；否则场景未就绪 /
    // 模型不可见时，垫子会停在上一帧的颜色上（表现为"撞地了还是绿的"这种残留）。
    let gst = "safe";
    let gclear = 0;
    const gMeas = !!(gOn && groundRoot && groundRoot.visible);
    if (gMeas) {
      this.groundBox.setFromObject(groundRoot);
      // ★ 净空 = 最低点世界高度 − **立足面**高度。机器人站在站台上时立足面是台面
      //   （refFloorY = 台面高度），否则"离地 120mm 的台面"会永远算成接近报警。
      gclear = this.groundBox.min.y - refFloorY;
      gst = classifyGround(gclear, g);       // 绿 / 黄 / 红 / 红闪
    }
    if (this.groundMesh) this.groundMesh.visible = gOn && this.group.visible;
    if (this.groundEdge) this.groundEdge.visible = gOn && this.group.visible;
    if (gOn) this._styleGround(gst, tSec, g);

    if (!this.cfg || this.cfg.enabled === false || !root || !root.visible) {
      if (this.helper) this.helper.visible = false;
      return out;
    }

    const box = this.box.setFromObject(root);
    let worst = null;
    const consider = (r) => {
      out.zones.push(r);
      if (r.state === "safe" && !worst) { worst = r; return; }
      if (r.state !== "safe" &&
        (!worst || STATE_RANK[r.state] > STATE_RANK[worst.state] ||
          (r.state === worst.state && r.clearance < worst.clearance))) {
        worst = r;
      }
    };

    for (const z of this.zones) {
      // ★ 逐面墙求值 + 逐面墙上色：只有"正在接近的那一面"变黄/变红。
      //   区域整体状态取**最严重的那一面墙**，保证"面板颜色"和"报警文案"始终一致。
      const edges = evaluateEdges(z.cfg, box);
      let worstE = null;
      z.walls.forEach((w, i) => {
        const e = edges[i] || { state: "safe", ratio: 1, clearance: 0 };
        w.state = e.state;
        this._styleWall(w, e.state, tSec);
        if (!worstE || STATE_RANK[e.state] > STATE_RANK[worstE.state] ||
          (e.state === worstE.state && e.clearance < worstE.clearance)) {
          worstE = e;
        }
      });
      const zr = worstE || evaluateZone(z.cfg, box);
      consider({ id: z.cfg.id || "", name: z.cfg.name || "", state: zr.state,
        ratio: zr.ratio, clearance: zr.clearance });
    }

    // 地面结果并入总状态（与各区域取更严重者）
    if (gMeas) {
      consider({ id: "ground", name: "地面", state: gst, clearance: gclear, ratio: 0 });
    }

    if (worst) {
      out.state = worst.state;
      out.ratio = Math.max(0, worst.ratio);
      out.clearance = worst.clearance;
      out.zoneId = worst.id;
      out.zoneName = worst.name;
    }
    if (this.helper && this.helper.visible) {
      this.helper.box = box;
      const c0 = this.zones[0] && this.zones[0].cfg.colors;
      _col.set(colorOf(worst ? worst.state : "safe", c0));
      this.helper.material.color.copy(_col);
    }
    return out;
  }

  /**
   * 状态 + 时间 → {颜色, 透明度, 自发光强度}。
   * ★ 按"状态"取色（`colorOf/opacityOf`），配置缺项时用**同状态的兜底色**，
   *   绝不回退成固定的安全绿 —— 否则接近/危险/碰撞会全是绿的。
   * 四级：绿(安全) → 黄(接近) → 红(危险) → 红闪(碰撞，用 cfg.blink 闪烁)。
   */
  _grade(state, tSec, colors, opacity) {
    const col = colorOf(state, colors);
    let op = opacityOf(state, opacity);
    let emis = state === "safe" ? 0.32 : 0.7;
    if (state === "hit") {
      const b = this.cfg.blink || {};
      const hz = num(b.hz, 4);
      const lo = num(b.min, 0.18);
      const hi = num(b.max, 0.63);
      const k = 0.5 + 0.5 * Math.sin(tSec * Math.PI * 2 * hz);
      op = lo + (hi - lo) * k;
      emis = 0.5 + 1.3 * k;
    }
    return { col, op, emis };
  }

  /** 给**一面墙**上色（它的面板 + 它自己的线框）。 */
  _styleWall(w, state, tSec) {
    const { col, op, emis } = this._grade(state, tSec, w.colors, w.opacity);
    _col.set(col);
    if (w.panelMat) {
      w.panelMat.color.copy(_col);
      w.panelMat.emissive.copy(_col);
      w.panelMat.opacity = op;
      w.panelMat.emissiveIntensity = emis;
    }
    if (w.lineMat) {
      w.lineMat.color.copy(_col);
      w.lineMat.opacity = Math.min(0.9, op * 2.2);
    }
  }

  /** 地面报警垫着色：与围栏墙同一套四态配色 + 闪烁（hit 红闪）。描边同步变色。 */
  _styleGround(state, tSec, g) {
    if (!this.groundMat) return;
    const { col, op, emis } = this._grade(state, tSec, g && g.colors, g && g.opacity);
    _col.set(col);
    this.groundMat.color.copy(_col);
    this.groundMat.emissive.copy(_col);
    this.groundMat.opacity = op;
    this.groundMat.emissiveIntensity = emis;
    if (this.groundEdgeMat) {
      this.groundEdgeMat.color.copy(_col);
      this.groundEdgeMat.opacity = Math.min(1, op * 2.4);
    }
  }

  dispose() {
    this._clear();
    if (this.helper) {
      this.helper.geometry.dispose();
      this.helper.material.dispose();
      this.helper = null;
    }
    if (this.parent) this.parent.remove(this.group);
  }
}
