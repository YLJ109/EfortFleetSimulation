// =====================================================================
// 地面报警垫回归（headless，用真实 THREE 构建 SafetyFence）
//
// 覆盖两类曾经出问题的地方：
//  A. 形状：报警垫必须是「四个角柱围成的那块地面」（区域多边形 / 矩形/正方形），
//     不是一个独立的圆盘。
//  B. 配色：四级 绿(安全)→黄(接近)→红(危险)→红闪(碰撞) 必须按**状态**取色；
//     即使配置里 ground.colors 缺失/不完整（旧配置文件、面板只改过一个字段、
//     后端还没补字段…），也绝不能全部回退成绿色。
//
// 运行： node tools/verify_ground.mjs
// =====================================================================
import * as THREE from "three";
import {
  SafetyFence, classifyGround, groundFootprint, zonePolygon,
  colorOf, opacityOf, FALLBACK_COLORS, evaluateEdges,
} from "../src/three/safety.js";
import { normalizeSafetyConfig } from "../src/utils/safetyConfig.js";

let pass = 0, fail = 0;
function ok(name, cond, extra = "") {
  if (cond) { pass++; console.log("  PASS  " + name); }
  else { fail++; console.log("  FAIL  " + name + (extra ? "   → " + extra : "")); }
}

const ZONE = {
  id: "z1", name: "主工作区", enabled: true, shape: "rect",
  center: { x: 0, z: 0 }, half: { x: 0.82, z: 0.82 }, radius: 0.9,
  height: 1.2, walls: true, posts: { enabled: true, size: 0.1, color: "#f2c500" },
  thresholds: { basis: "halfwidth", warn: 0.3, danger: 0.1, fixed_mm: 300 },
  colors: { safe: "#2ecc71", warn: "#f2c500", danger: "#e5484d", hit: "#ff2020" },
  opacity: { safe: 0.11, warn: 0.2, danger: 0.3, hit: 0.45 },
};

const GROUND_FULL = {
  enabled: true, warn_mm: 150, danger_mm: 80, hit_mm: 30,
  colors: { safe: "#2ecc71", warn: "#f2c500", danger: "#e5484d", hit: "#ff2020" },
  opacity: { safe: 0.10, warn: 0.22, danger: 0.34, hit: 0.50 },
};

function cfgWith(ground) {
  const c = {
    version: 1, enabled: true, zones: [JSON.parse(JSON.stringify(ZONE))],
    blink: { hz: 4, min: 0.18, max: 0.63 },
    alarm: { banner: true, chip: true, banner_min: "danger", sound: false },
    walls: true, overlay: { bbox: false },
  };
  if (ground !== undefined) c.ground = ground;
  return c;
}

/** 造一台"机器人"：groundRoot 的世界底面高度 = h 米。 */
function robotAt(h) {
  const scene = new THREE.Scene();
  const root = new THREE.Group();
  const groundRoot = new THREE.Group();
  const box = new THREE.Mesh(new THREE.BoxGeometry(0.3, 0.3, 0.3));
  box.position.y = 0.15 + h;         // 盒子高 0.3 → 底面 = h
  groundRoot.add(box);
  root.add(groundRoot);
  scene.add(root);
  scene.updateMatrixWorld(true);
  return { scene, root, groundRoot };
}

/** 应用配置 → 放到高度 h → 跑一帧 → 返回报警垫的实测状态。 */
function probe(cfg, h, tSec = 1.0) {
  const scene = new THREE.Scene();
  const fence = new SafetyFence(scene);
  fence.applyConfig(cfg);
  const { root, groundRoot } = robotAt(h);
  scene.add(root);
  scene.updateMatrixWorld(true);
  const r = fence.update(root, groundRoot, tSec);
  const hex = fence.groundMat ? fence.groundMat.color.getHexString() : "";
  const op = fence.groundMat ? fence.groundMat.opacity : -1;
  const vis = fence.groundMesh ? fence.groundMesh.visible : false;
  const edgeHex = fence.groundEdgeMat ? fence.groundEdgeMat.color.getHexString() : "";
  const box = fence.groundMesh
    ? new THREE.Box3().setFromObject(fence.groundMesh)
    : null;
  fence.dispose();
  return { r, hex, op, vis, edgeHex, box };
}

console.log("== 1. 分级阈值（纯函数，默认 接近150/危险80/碰撞30 mm） ==");
{
  const g = { warn_mm: 150, danger_mm: 80, hit_mm: 30 };
  ok("离地 0.60m → safe", classifyGround(0.60, g) === "safe");
  ok("离地 0.20m → safe", classifyGround(0.20, g) === "safe");
  ok("离地 0.15m → warn(取等)", classifyGround(0.15, g) === "warn");
  ok("离地 0.10m → warn", classifyGround(0.10, g) === "warn");
  ok("离地 0.08m → danger(取等)", classifyGround(0.08, g) === "danger");
  ok("离地 0.05m → danger", classifyGround(0.05, g) === "danger");
  ok("离地 0.03m → hit(取等)", classifyGround(0.03, g) === "hit");
  ok("离地 0.00m → hit", classifyGround(0.0, g) === "hit");
  ok("离地 -0.02m(穿地) → hit", classifyGround(-0.02, g) === "hit");
  ok("恰高于接近阈值 → safe", classifyGround(0.1501, g) === "safe");
  ok("缺省阈值兜底可用", ["safe", "warn", "danger", "hit"].includes(classifyGround(0.5, {})));
}

console.log("\n== 2. 四态配色：配置完整时必须按状态取色 ==");
{
  const exp = [["0.60", "safe", "2ecc71"], ["0.12", "warn", "f2c500"],
               ["0.06", "danger", "e5484d"], ["0.01", "hit", "ff2020"]];
  for (const [hh, st, hex] of exp) {
    const p = probe(cfgWith(Object.assign({}, GROUND_FULL)), parseFloat(hh));
    ok(`离地 ${hh}m → ${st} 且颜色 #${hex}`, p.r.state === st && p.hex === hex,
      `state=${p.r.state} hex=${p.hex}`);
  }
}

console.log("\n== 3. 四态配色：配置缺 colors/opacity 也必须正确（曾经的绿色 bug） ==");
{
  const noColors = Object.assign({}, GROUND_FULL);
  delete noColors.colors;
  delete noColors.opacity;
  const exp = [["0.60", "safe", "2ecc71"], ["0.12", "warn", "f2c500"],
               ["0.06", "danger", "e5484d"], ["0.01", "hit", "ff2020"]];
  for (const [hh, st, hex] of exp) {
    const p = probe(cfgWith(JSON.parse(JSON.stringify(noColors))), parseFloat(hh));
    ok(`缺 colors：离地 ${hh}m → ${st} 颜色 #${hex}`, p.hex === hex,
      `state=${p.r.state} hex=${p.hex}`);
  }
  const partial = Object.assign({}, GROUND_FULL, { colors: { safe: "#2ecc71" } });
  const p = probe(cfgWith(partial), 0.01);
  ok("colors 只有 safe 时，hit 仍取红色兜底", p.hex === "ff2020", `hex=${p.hex}`);
}

console.log("\n== 4. 旧配置（整块缺 ground / 缺 colors）归一化后必须能正常分级报警 ==");
{
  // 本项目线上 config/safety.json 就是"没有 ground 块"的老配置。
  // 引擎本身不凭空造配置（那是配置层职责）→ 缺块时不建垫子、也不能报错。
  const p0 = probe(cfgWith(undefined), 0.01);
  ok("缺 ground 块：引擎不报错、不建垫子", p0.hex === "" && p0.vis === false,
    `hex=${p0.hex} vis=${p0.vis}`);

  // 真实链路：store 用 normalizeSafetyConfig 补齐 → 垫子存在且四态颜色正确
  const legacy = { zones: [JSON.parse(JSON.stringify(ZONE))] };
  const norm = normalizeSafetyConfig(legacy);
  ok("归一化补出 ground.colors 四色", Object.keys(norm.ground.colors || {}).length === 4);
  ok("归一化补出 ground.opacity 四值", Object.keys(norm.ground.opacity || {}).length === 4);
  ok("归一化后地面阈值齐全",
    Number.isFinite(norm.ground.warn_mm) &&
    Number.isFinite(norm.ground.danger_mm) &&
    Number.isFinite(norm.ground.hit_mm));

  const exp = [["0.60", "safe", "2ecc71"], ["0.12", "warn", "f2c500"],
               ["0.06", "danger", "e5484d"], ["0.01", "hit", "ff2020"]];
  for (const [hh, st, hex] of exp) {
    const p = probe(norm, parseFloat(hh));
    ok(`旧配置归一化后：离地 ${hh}m → ${st} #${hex}`,
      p.r.state === st && p.hex === hex, `state=${p.r.state} hex=${p.hex}`);
  }

  // 区域缺 colors/opacity 同样要补齐（否则围栏面板也是"全绿"）
  const legacy2 = { zones: [Object.assign(JSON.parse(JSON.stringify(ZONE)), { colors: undefined, opacity: undefined })] };
  const norm2 = normalizeSafetyConfig(legacy2);
  ok("归一化补出区域 colors", Object.keys(norm2.zones[0].colors || {}).length === 4);
  ok("归一化补出区域 opacity", Object.keys(norm2.zones[0].opacity || {}).length === 4);
}

console.log("\n== 5. 形状：报警垫 = 四个角柱围成的区域（矩形，不是圆盘） ==");
{
  const cfg = cfgWith(Object.assign({}, GROUND_FULL));
  // 纯函数：footprint 必须等于区域多边形
  const fp = groundFootprint(cfg);
  const zp = zonePolygon(cfg.zones[0]);
  ok("groundFootprint = 区域多边形顶点数(4)", fp.length === 4, `len=${fp.length}`);
  ok("groundFootprint 与 zonePolygon 顶点一致",
    JSON.stringify(fp) === JSON.stringify(zp), JSON.stringify(fp));

  const p = probe(cfg, 0.60);
  ok("报警垫为矩形：X 向跨度 ≈ 1.64m",
    p.box && Math.abs(p.box.max.x - p.box.min.x - 1.64) < 1e-6,
    p.box ? `dx=${(p.box.max.x - p.box.min.x).toFixed(4)}` : "no box");
  ok("报警垫为矩形：Z 向跨度 ≈ 1.64m",
    p.box && Math.abs(p.box.max.z - p.box.min.z - 1.64) < 1e-6,
    p.box ? `dz=${(p.box.max.z - p.box.min.z).toFixed(4)}` : "no box");
  ok("报警垫贴合地面（y≈0.004）",
    p.box && Math.abs(p.box.min.y - 0.004) < 1e-6 && Math.abs(p.box.max.y - 0.004) < 1e-6,
    p.box ? `y=[${p.box.min.y},${p.box.max.y}]` : "no box");

  // 换一个"长方形"（half.x≠half.z）区域，垫子要跟着变长
  const cfg2 = cfgWith(Object.assign({}, GROUND_FULL));
  cfg2.zones[0].half = { x: 1.2, z: 0.5 };
  const p2 = probe(cfg2, 0.60);
  ok("长方形区域(2.4m × 1.0m)垫子跟随",
    Math.abs((p2.box.max.x - p2.box.min.x) - 2.4) < 1e-6 &&
    Math.abs((p2.box.max.z - p2.box.min.z) - 1.0) < 1e-6,
    `dx=${(p2.box.max.x - p2.box.min.x).toFixed(4)} dz=${(p2.box.max.z - p2.box.min.z).toFixed(4)}`);

  // 圆形区域 → 垫子也应是圆（多边形近似）
  const cfg3 = cfgWith(Object.assign({}, GROUND_FULL));
  cfg3.zones[0].shape = "circle";
  cfg3.zones[0].radius = 0.9;
  const p3 = probe(cfg3, 0.60);
  ok("圆形区域垫子直径 ≈ 1.8m",
    Math.abs((p3.box.max.x - p3.box.min.x) - 1.8) < 0.02,
    `dx=${(p3.box.max.x - p3.box.min.x).toFixed(4)}`);
}

console.log("\n== 6. 地面报警与围栏报警合并（取更严重者） ==");
{
  const cfg = cfgWith(Object.assign({}, GROUND_FULL));
  const scene = new THREE.Scene();
  const fence = new SafetyFence(scene);
  fence.applyConfig(cfg);
  // 机器人在围栏外 → 围栏应报 hit；同时离地很高 → 地面 safe
  const root = new THREE.Group();
  const m = new THREE.Mesh(new THREE.BoxGeometry(0.2, 0.2, 0.2));
  m.position.set(4, 1.0, 4);
  root.add(m);
  const g = new THREE.Group();
  const gm = new THREE.Mesh(new THREE.BoxGeometry(0.2, 0.2, 0.2));
  gm.position.set(4, 1.0, 4);
  g.add(gm);
  scene.add(root, g);
  scene.updateMatrixWorld(true);
  const r = fence.update(root, g, 1.0);
  ok("围栏越界 → 总状态 hit", r.state === "hit", `state=${r.state}`);
  ok("地面分区结果被计入 zones", r.zones.some((z) => z.id === "ground"));
  fence.dispose();
}

console.log("\n== 7. ★ 逐面墙着色：只有「正在接近的那一面墙」变黄/变红（不是四面一起变） ==");
{
  const cfg = cfgWith(Object.assign({}, GROUND_FULL));
  const scene = new THREE.Scene();
  const fence = new SafetyFence(scene);
  fence.applyConfig(cfg);

  // 区域 = 1.64m 方形。边序来自 zonePolygon(BL,BR,TR,TL)，故：
  //   墙 0 = -Z(下)   墙 1 = +X(右)   墙 2 = +Z(上)   墙 3 = -X(左)
  const W = { "-Z(下)": 0, "+X(右)": 1, "+Z(上)": 2, "-X(左)": 3 };

  /** 把一个 0.1m 的方块放在 (x,z)，跑一帧，读回四面墙各自的状态与颜色 */
  const probeWall = (x, z) => {
    const root = new THREE.Group();
    const m = new THREE.Mesh(new THREE.BoxGeometry(0.1, 0.1, 0.1));
    m.position.set(x, 0.6, z);
    root.add(m);
    const gr = new THREE.Group();
    const gm = new THREE.Mesh(new THREE.BoxGeometry(0.1, 0.1, 0.1));
    gm.position.set(x, 1.6, z);          // 离地很高 → 地面始终 safe，只看围栏
    gr.add(gm);
    scene.add(root, gr);
    scene.updateMatrixWorld(true);
    const r = fence.update(root, gr, 1.0);
    scene.remove(root, gr);
    const ws = fence.zones[0].walls;
    return {
      r,
      states: ws.map((w) => w.state),
      hexes: ws.map((w) => w.panelMat.color.getHexString()),
    };
  };
  const hot = (p) => p.states.map((s, i) => (s === "safe" ? -1 : i)).filter((i) => i >= 0);

  // (1) 居中 → 四面墙都应该是绿的（不能无脑全亮）
  const c0 = probeWall(0, 0);
  ok("居中时四面墙全绿", c0.states.every((s) => s === "safe") &&
    c0.hexes.every((h) => h === "2ecc71"), JSON.stringify(c0.states));

  // (2) 靠近 +X 墙（余量 40mm → 危险）→ 只有 +X 那面红，其余三面仍绿
  const c1 = probeWall(0.73, 0);
  ok("靠近 +X 墙：只有 +X 这一面非 safe",
    hot(c1).length === 1 && c1.states[W["+X(右)"]] !== "safe",
    `hot=${JSON.stringify(hot(c1))} states=${JSON.stringify(c1.states)}`);
  ok("靠近 +X 墙：+X 墙取危险红 #e5484d", c1.hexes[W["+X(右)"]] === "e5484d", c1.hexes[1]);
  ok("靠近 +X 墙：其余三面仍是安全绿 #2ecc71",
    [0, 2, 3].every((i) => c1.hexes[i] === "2ecc71"), JSON.stringify(c1.hexes));
  ok("区域整体状态 = 最严重的那面墙(danger)", c1.r.state === "danger", c1.r.state);

  // (3) 换到 -Z 墙 → 报警的那一面跟着换，+X 墙恢复绿
  const c2 = probeWall(0, -0.73);
  ok("靠近 -Z 墙：只有 -Z 这一面非 safe",
    hot(c2).length === 1 && c2.states[W["-Z(下)"]] !== "safe",
    `hot=${JSON.stringify(hot(c2))}`);
  ok("靠近 -Z 墙：+X 墙已恢复安全绿", c2.hexes[W["+X(右)"]] === "2ecc71", c2.hexes[1]);
  ok("靠近 -Z 墙：-Z 墙才是红的", c2.hexes[W["-Z(下)"]] === "e5484d", c2.hexes[0]);

  // (4) 离得远一点（余量 170mm → 接近）→ 该面变黄
  const c3 = probeWall(0.60, 0);
  ok("离 +X 墙 170mm：只有 +X 一面变黄 #f2c500",
    c3.hexes[W["+X(右)"]] === "f2c500" && hot(c3).length === 1,
    `hot=${JSON.stringify(hot(c3))} hexes=${JSON.stringify(c3.hexes)}`);

  // (5) 越过 +X 墙 → 只有那一面 hit 红闪
  const c4 = probeWall(0.85, 0);
  ok("越过 +X 墙：只有 +X 一面 hit 红闪 #ff2020",
    c4.states[W["+X(右)"]] === "hit" && c4.hexes[W["+X(右)"]] === "ff2020" &&
    hot(c4).length === 1,
    `states=${JSON.stringify(c4.states)} hexes=${JSON.stringify(c4.hexes)}`);

  // (6) 纯函数：逐墙条数 = 边数，且指向正确的那一面
  const box = new THREE.Box3(new THREE.Vector3(-0.78, 0.55, -0.05),
                             new THREE.Vector3(-0.68, 0.65, 0.05));
  const edges = evaluateEdges(cfg.zones[0], box);
  ok("evaluateEdges 一面墙一项（矩形=4）", edges.length === 4, String(edges.length));
  ok("贴在 -X 侧时只有第 4 面(-X)非 safe",
    edges[W["-X(左)"]].state !== "safe" &&
    edges.filter((e) => e.state !== "safe").length === 1,
    JSON.stringify(edges.map((e) => e.state)));

  // (7) 关掉面板后，墙的线框仍逐面着色（walls=false 只隐藏面板）
  const cfgNoPanel = cfgWith(Object.assign({}, GROUND_FULL));
  cfgNoPanel.zones[0].walls = false;
  const f2 = new SafetyFence(new THREE.Scene());
  f2.applyConfig(cfgNoPanel);
  ok("关掉面板：墙对象仍存在（线框照常逐面着色）",
    f2.zones[0].walls.length === 4 &&
    f2.zones[0].walls.every((w) => !!w.lineMat), String(f2.zones[0].walls.length));
  f2.dispose();
  fence.dispose();
}

console.log("\n== 8. ★ 视觉升级：垫子状态描边 + 角柱细节件 ==");
{
  const cfg = cfgWith(Object.assign({}, GROUND_FULL));

  // (1) 角柱：底板/螺栓/警示柱脚/反光带/顶盖 都在，且柱身颜色取 posts.color
  const f = new SafetyFence(new THREE.Scene());
  f.applyConfig(cfg);
  const cnt = {};
  f.zones[0].group.traverse((o) => { if (o.name) cnt[o.name] = (cnt[o.name] || 0) + 1; });
  ok("四根柱身", cnt["post-body"] === 4, `post-body=${cnt["post-body"]}`);
  ok("四块底板 + 16 颗螺栓", cnt["post-base"] === 4 && cnt["post-bolt"] === 16,
    `base=${cnt["post-base"]} bolt=${cnt["post-bolt"]}`);
  ok("四段警示柱脚 + 四条反光带 + 四个顶盖",
    cnt["post-warnband"] === 4 && cnt["post-reflect"] === 4 && cnt["post-cap"] === 4,
    `warn=${cnt["post-warnband"]} ref=${cnt["post-reflect"]} cap=${cnt["post-cap"]}`);
  ok("柱身颜色取 posts.color(#f2c500)",
    f.zones[0].postMat.color.getHexString() === "f2c500",
    f.zones[0].postMat.color.getHexString());
  ok("角柱细节材质随区域统一回收(无泄漏)", f.zones[0].postMats.length === 5,
    String(f.zones[0].postMats.length));
  f.dispose();

  // (2) 垫子描边存在，且**随四级状态变色**（与垫面同一套取色）
  const p1 = probe(cfgWith(Object.assign({}, GROUND_FULL)), 0.60);
  ok("状态描边存在且非空色", !!p1.edgeHex, `edgeHex=${p1.edgeHex}`);
  for (const [hh, st, hex] of [["0.60", "safe", "2ecc71"], ["0.12", "warn", "f2c500"],
                               ["0.06", "danger", "e5484d"], ["0.01", "hit", "ff2020"]]) {
    const p = probe(cfgWith(Object.assign({}, GROUND_FULL)), parseFloat(hh));
    ok(`描边 ${st} → #${hex}`, p.edgeHex === hex && p.r.state === st,
      `state=${p.r.state} edgeHex=${p.edgeHex}`);
  }
  // (3) 描边与垫面**同色**（描边只是更醒目，不得出现"垫黄边红"这种不一致）
  const p2 = probe(cfgWith(Object.assign({}, GROUND_FULL)), 0.12);
  ok("描边与垫面同色（不会垫黄边红）", p2.edgeHex === p2.hex,
    `pad=${p2.hex} edge=${p2.edgeHex}`);
}

console.log("\n== 9. 兜底色表自洽 ==");
{
  ok("FALLBACK_COLORS 四态齐全且互不相同",
    new Set(Object.values(FALLBACK_COLORS)).size === 4);
  ok("hit 兜底色为红色系", /^#ff/i.test(FALLBACK_COLORS.hit), FALLBACK_COLORS.hit);
  ok("colorOf 未知状态回退 safe", colorOf("nope", {}) === FALLBACK_COLORS.safe);
  ok("opacityOf 未知状态回退数字", Number.isFinite(opacityOf("nope", {})));
}

console.log("\n== 10. ★ 世界矩阵新鲜度：还没渲染过也绝不能假报碰撞 ==");
{
  // 现场事故（2026-10-12）：真实监控页开机即挂「碰撞报警！机器人已触及台面」，
  // 而机器人离台面实际还有两百多毫米。根因不在阈值，也不在模型：
  //   Box3.setFromObject 只刷新**对象自己**的矩阵，父级矩阵要等 renderer.render()
  //   里的 scene.updateMatrixWorld() 才算好。于是"没渲染过就测量"的两种场合
  //   —— 开机首帧、以及页面在后台（App.vue 的循环 document.hidden 时整个停帧，
  //   首帧的假结论再没人纠正）—— 量到的是**没翻转、没抬升**的局部坐标：
  //   root 的 rotX(-90)+站台 180mm 全部丢失，轴承环的半径 0.132 直接变成 -0.132m
  //   的"净空" → -312mm → hit。
  // 上面 1~9 节的夹具都在测量前调过 scene.updateMatrixWorld(true)，
  // 所以它们**天生看不见**这个坑 —— 本节故意不调。
  const PLATFORM_H = 0.18;
  /** 与 robotModel.js 同构：root 做 DH+Z→Three+Y 的 -90° 翻转并抬上台面，J1 挂轴承环。 */
  const build = () => {
    const scene = new THREE.Scene();
    const root = new THREE.Group();
    root.name = "robot-root";
    root.rotation.x = -Math.PI / 2;
    root.position.y = PLATFORM_H;
    const j1 = new THREE.Group();
    j1.name = "J1";
    const ring = new THREE.Mesh(
      new THREE.CylinderGeometry(0.132, 0.132, 0.012, 36), new THREE.MeshBasicMaterial());
    ring.rotation.x = Math.PI / 2;
    ring.position.z = 0.331;                       // d1 - 0.045
    j1.add(ring);
    root.add(j1);
    scene.add(root);
    return { scene, root, j1 };
  };
  // 真实净空（世界矩阵算好后）：环轴向沿局部 Z → 世界 y = 0.18 + (0.331 - 0.006)
  const TRUE_CLR = 0.331 - 0.006;

  const fence = new SafetyFence(new THREE.Scene());
  fence.applyConfig(normalizeSafetyConfig(cfgWith(Object.assign({}, GROUND_FULL))));

  const a = build();                                // ① 首帧：不碰 updateMatrixWorld
  const rA = fence.update(a.root, a.j1, 1.0, PLATFORM_H);
  const clrA = fence.groundBox.min.y - PLATFORM_H;   // 地面这一路的实测净空
  const b = build();
  b.scene.updateMatrixWorld(true);                  // ② 同一棵树的"渲染过"版本
  const rB = fence.update(b.root, b.j1, 1.0, PLATFORM_H);
  const clrB = fence.groundBox.min.y - PLATFORM_H;
  fence.dispose();

  ok("首帧（未渲染）不报地面碰撞", rA.zoneId !== "ground",
    `state=${rA.state} zone=${rA.zoneId} 地面净空=${(clrA * 1000).toFixed(0)}mm`);
  ok("首帧不判 hit", rA.state !== "hit", `state=${rA.state}`);
  ok("首帧地面净空 == 渲染后地面净空（一毫米都不许差）",
    Math.abs(clrA - clrB) < 1e-9, `A=${clrA.toFixed(4)} B=${clrB.toFixed(4)}`);
  ok("地面净空 == 真值 325mm（口径本身没漂）",
    Math.abs(clrB - TRUE_CLR) < 1e-6 && Math.abs(clrA - TRUE_CLR) < 1e-6,
    `A=${clrA.toFixed(4)} B=${clrB.toFixed(4)} 期望=${TRUE_CLR.toFixed(4)}`);
  ok("该姿态总判 safe（离台面 325mm > 接近阈值 150mm）",
    rA.state === "safe" && rB.state === "safe", `A=${rA.state} B=${rB.state}`);
}

console.log(`\n结果: ${pass} PASS / ${fail} FAIL`);
process.exit(fail ? 1 : 0);
