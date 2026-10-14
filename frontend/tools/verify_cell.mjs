// =====================================================================
// 工作区站台（cell platform）回归（headless，真实 THREE）
//
// 覆盖两件曾在浏览器里"看着不对但说不清哪儿错"的事：
//  A. 站台几何：台面**顶面必须正好在 y = PLATFORM_H**（机器人立足面），
//     台面必须居中、必须小于工作区地坪、接触阴影必须落在台面**之上**。
//     —— 差 1~2cm 时机器人就会"陷进台面里"或"悬空"，肉眼几乎看不出来。
//  B. 立足面零点（refFloorY）：机器人站到台面上以后，地面碰撞检测的
//     "离地 0mm" 必须指**台面**而不是房间地坪；否则 12cm 的台面本身
//     就会把离地距离恒定抬到 120mm → 永远显示"接近"黄灯（修复前的表现）。
//
// 运行： node tools/verify_cell.mjs
// =====================================================================
import * as THREE from "three";
import {
  buildCell, PLATFORM_H, PLATFORM_SIZE, PLATFORM_TOP_SIZE, FLOOR_SIZE, HAZARD_TILE,
} from "../src/three/cell.js";
import { SafetyFence } from "../src/three/safety.js";
import { buildRobot } from "../src/robot/robotModel.js";
import { FALLBACK_DH } from "../src/config.js";
// ★ D5′ 同源钉要读仓库根的 yaml 与后端源码（第 7 节），故需要 fs/path
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
const root = dirname(dirname(fileURLToPath(import.meta.url)));   // → frontend/

// ---------------------------------------------------------------------
// cell.js 用 document.createElement("canvas") 生成程序化贴图（地坪/阴影）。
// headless 下给个最小打桩：2D 上下文的方法全部空实现，渐变对象只需 addColorStop。
// 只影响贴图像素，不影响本测试关心的几何与坐标。
// ---------------------------------------------------------------------
if (typeof globalThis.document === "undefined") {
  const grad = { addColorStop() {} };
  const ctx = new Proxy({}, {
    get(_t, k) {
      if (k === "createRadialGradient" || k === "createLinearGradient") return () => grad;
      if (k === "canvas") return null;
      return () => {};
    },
    set() { return true; },
  });
  globalThis.document = {
    createElement() {
      return { width: 0, height: 0, getContext: () => ctx };
    },
  };
}

let pass = 0, fail = 0;
function ok(name, cond, extra = "") {
  if (cond) { pass++; console.log("  PASS  " + name); }
  else { fail++; console.log("  FAIL  " + name + (extra ? "   → " + extra : "")); }
}
const f3 = (v) => (Math.round(v * 1000) / 1000).toFixed(3);

const cell = buildCell();
const g = cell.group;
g.updateMatrixWorld(true);

function parts(name) {
  const out = [];
  g.traverse((o) => { if (o.name === name) out.push(o); });
  return out;
}
const wbox = (o) => new THREE.Box3().setFromObject(o);

console.log("== 1. ★ 站台几何：台面顶面严格在 PLATFORM_H ==");
{
  const top = parts("platform-top");
  ok("有 1 块台面板", top.length === 1, String(top.length));
  const tb = wbox(top[0]);
  ok(`台面板顶面 y = PLATFORM_H(${f3(PLATFORM_H)}m)`,
    Math.abs(tb.max.y - PLATFORM_H) < 0.004, `maxY=${f3(tb.max.y)}`);

  const body = parts("platform-body");
  ok("有 1 个台身", body.length === 1, String(body.length));
  const bb = wbox(body[0]);
  // ★ 站台不能是"从地面长出来的一块光板"：台身必须被踢脚底盘托起（离地），
  //   否则看起来只是在地上铺了个方盒子，完全不像设备基座。
  ok("★ 台身被底盘托起、离地（有设备基座的层次感）", bb.min.y > 0.05,
    `minY=${f3(bb.min.y)}`);
  ok("台身顶面不乱顶出台面板", bb.max.y <= tb.max.y + 0.004, `bodyMaxY=${f3(bb.max.y)}`);

  // 站台最底下的部件必须贴地，否则整块站台"浮"在空中
  const lowest = Math.min(...["platform-foot", "platform-plinth", "platform-body"]
    .map((n) => wbox(parts(n)[0]).min.y));
  ok("站台最底部贴地（调平脚落在 y ≈ 0）", Math.abs(lowest) < 0.004,
    `lowest=${f3(lowest)}`);
  ok("站台高度 = PLATFORM_H", Math.abs(tb.max.y - lowest - PLATFORM_H) < 0.005,
    `实际高=${f3(tb.max.y - lowest)}`);

  // 居中：站台必须正对工作区原点，否则机器人站偏
  const c = tb.getCenter(new THREE.Vector3());
  ok("站台居中于工作区原点", Math.abs(c.x) < 0.01 && Math.abs(c.z) < 0.01,
    `center=(${f3(c.x)}, ${f3(c.z)})`);

  // 台面小于工作区地坪（否则台面会盖住整块地坪，失去"站台"观感）
  const sx = tb.max.x - tb.min.x, sz = tb.max.z - tb.min.z;
  ok(`台面(${f3(sx)}×${f3(sz)}m) 小于工作区地坪(${FLOOR_SIZE}m)`,
    sx < FLOOR_SIZE && sz < FLOOR_SIZE, `${f3(sx)}×${f3(sz)} vs ${FLOOR_SIZE}`);
  ok("台面尺寸符合常量 PLATFORM_TOP_SIZE(台身 + 7cm 台沿)",
    Math.abs(sx - PLATFORM_TOP_SIZE) < 0.01, `sx=${f3(sx)} vs ${f3(PLATFORM_TOP_SIZE)}`);
  // 台面必须完全落在工作区地坪范围内
  ok("台面完全落在工作区地坪内",
    Math.abs(tb.min.x) < FLOOR_SIZE / 2 && Math.abs(tb.min.z) < FLOOR_SIZE / 2);
}

console.log("\n== 2. 设备基座细节：斜纹带 / 底盘 / 调平脚 / 檐口 / 铭牌 / 接触阴影 ==");
{
  // ---- 黄黑警示斜纹带 ----
  const band = parts("platform-band");
  ok("有 1 圈警示带", band.length === 1, String(band.length));
  const bm = band[0].material;
  ok("★ 警示带是**黄黑斜纹贴图**（不是一条纯色黄带）", !!bm.map, `map=${!!bm.map}`);
  const gp = band[0].geometry.parameters;
  const rep = bm.map.repeat;
  ok("★ 斜纹等比例（宽/高方向单元同尺寸，不会被拉成横条）",
    Math.abs(gp.width / rep.x - gp.height / rep.y) < 0.005,
    `单元=${f3(gp.width / rep.x)}×${f3(gp.height / rep.y)}m 应=${HAZARD_TILE}`);
  ok("斜纹单元边长符合 HAZARD_TILE",
    Math.abs(gp.width / rep.x - HAZARD_TILE) < 0.005, `单元=${f3(gp.width / rep.x)}`);
  const nbb = wbox(band[0]);
  ok("警示带在台身范围内(0 < y < PLATFORM_H)",
    nbb.min.y > 0 && nbb.max.y < PLATFORM_H, `[${f3(nbb.min.y)}, ${f3(nbb.max.y)}]`);

  // ---- 踢脚底盘 + 台身（"下窄上宽"的基座断面） ----
  const bodyB = wbox(parts("platform-body")[0]);
  const plinth = parts("platform-plinth");
  ok("有 1 个踢脚底盘", plinth.length === 1, String(plinth.length));
  const pb = wbox(plinth[0]);
  ok("★ 底盘比台身内收（不是上下一样粗的方柱）",
    (pb.max.x - pb.min.x) < (bodyB.max.x - bodyB.min.x) - 0.1,
    `底盘=${f3(pb.max.x - pb.min.x)} 台身=${f3(bodyB.max.x - bodyB.min.x)}`);
  ok("底盘正好托在台身正下方（两者衔接、不留缝）",
    Math.abs(pb.max.y - bodyB.min.y) < 0.005,
    `底盘顶=${f3(pb.max.y)} 台身底=${f3(bodyB.min.y)}`);

  // ---- 调平脚 ----
  ok("有 4 个调平脚 + 4 根支脚螺栓",
    parts("platform-foot").length === 4 && parts("platform-footbolt").length === 4,
    `${parts("platform-foot").length} / ${parts("platform-footbolt").length}`);
  const feet = parts("platform-foot");
  // ★ 取 4 只脚里最大的 |x|（脚分居四角，第 0 只的 x 是负的，直接读 max.x 会得到负数）
  const footReach = Math.max(...feet.map((o) => wbox(o).max.x));
  const plinthHalf = (pb.max.x - pb.min.x) / 2;
  ok("★ 调平脚从底盘下方探出（看得见，不是藏在里面）",
    footReach > plinthHalf, `脚外缘=${f3(footReach)} 底盘半宽=${f3(plinthHalf)}`);

  // ---- 台面檐口 ----
  const rim = parts("platform-toprim");
  ok("有 1 圈台面檐口", rim.length === 1, String(rim.length));
  const rb = wbox(rim[0]);
  ok("★ 檐口比台身外挑（台面下方投出一条暗线，不是平齐的方盒子）",
    (rb.max.x - rb.min.x) > (bodyB.max.x - bodyB.min.x) + 0.05,
    `檐口=${f3(rb.max.x - rb.min.x)} 台身=${f3(bodyB.max.x - bodyB.min.x)}`);
  ok("檐口外挑不超过围栏角柱内缘(0.77m)，不顶到柱子",
    rb.max.x <= 0.77, `檐口半宽=${f3(rb.max.x)}`);

  // ---- 铭牌：必须夹在警示带与檐口之间，上下都不被埋 ----
  const plate = parts("platform-plate");
  ok("有 1 块铭牌", plate.length === 1, String(plate.length));
  const plb = wbox(plate[0]);
  ok("★ 铭牌夹在警示带与台面檐口之间（上下都不被埋进去）",
    plb.min.y > nbb.max.y - 0.001 && plb.max.y <= rb.min.y + 0.001,
    `铭牌=[${f3(plb.min.y)}, ${f3(plb.max.y)}] 带顶=${f3(nbb.max.y)} 檐底=${f3(rb.min.y)}`);
  ok("铭牌贴在台身外侧（突出台身面，不被遮）",
    plb.max.z > bodyB.max.z, `铭牌外=${f3(plb.max.z)} 台身面=${f3(bodyB.max.z)}`);

  // ---- 接触阴影 ----
  const sh = parts("floor-shadow");
  ok("有 1 片接触阴影", sh.length === 1, String(sh.length));
  if (sh.length) {
    const sb = wbox(sh[0]);
    ok("★ 阴影在台面**之上**（不是陷在台里）",
      sb.min.y > PLATFORM_H - 0.004, `minY=${f3(sb.min.y)} ref=${f3(PLATFORM_H)}`);
    ok("阴影小于台面（不会糊出台沿）",
      (sb.max.x - sb.min.x) < PLATFORM_TOP_SIZE, `w=${f3(sb.max.x - sb.min.x)}`);
  }
}

console.log("\n== 3. ★ 机器人真的站在台面上（用真实 FK 模型实测） ==");
{
  const { root, joints } = buildRobot(FALLBACK_DH, "floor");
  // buildRobot 必须自己先摆好零位（theta_offset 已应用），否则这里是"折臂下垂"的怪姿态
  root.position.y = PLATFORM_H;      // 与 manager.initRobots 的抬升一致
  const scene = new THREE.Scene();
  scene.add(root);
  scene.updateMatrixWorld(true);

  const all = new THREE.Box3().setFromObject(root);
  // 底板（含 3mm 锚栓外露）必须落在台面**上**：既不穿进台里，也不悬空
  ok("★ 机器人坐落在台面上（不悬空、不穿台）",
    all.min.y >= PLATFORM_H - 0.008 && all.min.y <= PLATFORM_H + 0.01,
    `minY=${f3(all.min.y)} 台面=${f3(PLATFORM_H)}`);
  ok("机器人整体在台面之上（举高可用净空 ≥ 0.8m）",
    all.max.y - all.min.y > 0.8, `高=${f3(all.max.y - all.min.y)}`);

  // J1 转塔（getGroundRoot()）在零位时必须高于台面 —— 底座不能算进"离地高度"
  const jb = new THREE.Box3().setFromObject(joints[0]);
  ok("J1 子树最低点高于台面（贴地底座已被排除）",
    jb.min.y > PLATFORM_H + 0.15, `minY=${f3(jb.min.y)}`);

  console.log("\n== 4. ★ 立足面零点 refFloorY：离地 0mm 指台面，不指房间地坪 ==");
  {
    const cfg = {
      version: 1, enabled: true,
      zones: [{
        id: "z1", name: "主工作区", enabled: true, shape: "rect",
        center: { x: 0, z: 0 }, half: { x: 0.82, z: 0.82 }, radius: 0.9,
        height: 1.2, walls: true, posts: { enabled: true, size: 0.1, color: "#f2c500" },
        thresholds: { basis: "halfwidth", warn: 0.3, danger: 0.1, fixed_mm: 300 },
      }],
      blink: { hz: 4, min: 0.18, max: 0.63 },
      alarm: { banner: true, chip: true, banner_min: "danger", sound: false },
      walls: true, ground: { enabled: true, warn_mm: 150, danger_mm: 80, hit_mm: 30 },
      overlay: { bbox: false },
    };
    /** 造一个"底面离台面 delta 米"的检测体。 */
    const probeAt = (delta) => {
      const s = new THREE.Scene();
      const r = new THREE.Group();
      const gr = new THREE.Group();
      const bx = new THREE.Mesh(new THREE.BoxGeometry(0.3, 0.3, 0.3));
      bx.position.y = 0.15 + PLATFORM_H + delta;
      gr.add(bx); r.add(gr); s.add(r);
      s.updateMatrixWorld(true);
      return { scene: s, root: r, groundRoot: gr };
    };

    // 最贴近台面（离台面 10mm）→ 传 refFloorY=台面 → hit 红闪
    const p = probeAt(0.01);
    const f1 = new SafetyFence(new THREE.Scene());
    f1.applyConfig(cfg);
    const r1 = f1.update(p.root, p.groundRoot, 1.0, PLATFORM_H);
    ok("传 refFloorY=台面 → 离台面 10mm 判 hit",
      Math.abs(r1.clearance - 0.01) < 0.02 && r1.zoneId === "ground" && r1.state === "hit",
      `clearance=${f3(r1.clearance)} state=${r1.state} zone=${r1.zoneId}`);
    f1.dispose();

    // 回归（修复前的 bug）：不传 refFloorY → 台面高度(PLATFORM_H)被算进"离地距离"。
    // ★ 这里必须**只留地面通道**（zones: []）：否则区域围栏也会返回一个 safe 结果，
    //   两者同为 safe 时谁都不会替换谁，"地面读数"就被区域读数盖掉、量不到真值。
    const cfgGroundOnly = Object.assign({}, cfg, { zones: [] });
    const f2 = new SafetyFence(new THREE.Scene());
    f2.applyConfig(cfgGroundOnly);
    const r2 = f2.update(p.root, p.groundRoot, 1.0);
    ok("★ 不传 refFloorY → 把台面高度算进离地距离（读数虚高整整一个 PLATFORM_H）",
      Math.abs(r2.clearance - (PLATFORM_H + 0.01)) < 0.02 && r2.zoneId === "ground",
      `clearance=${f3(r2.clearance)} 真值=${f3(0.01)} 台高=${f3(PLATFORM_H)} state=${r2.state}`);
    f2.dispose();

    // 同一条件传了 refFloorY 才对得上 —— 证明上面那个偏差确实来自零点
    const f2b = new SafetyFence(new THREE.Scene());
    f2b.applyConfig(cfgGroundOnly);
    const r2b = f2b.update(p.root, p.groundRoot, 1.0, PLATFORM_H);
    ok("同一条件传 refFloorY=台面 → 读数回到真值 ≈10mm（判 hit）",
      Math.abs(r2b.clearance - 0.01) < 0.02 && r2b.state === "hit",
      `clearance=${f3(r2b.clearance)} state=${r2b.state}`);
    f2b.dispose();

    // 抬到台面上方 0.6m → 相对台面 safe
    const q = probeAt(0.6);
    const f3f = new SafetyFence(new THREE.Scene());
    f3f.applyConfig(cfg);
    const r3 = f3f.update(q.root, q.groundRoot, 1.0, PLATFORM_H);
    ok("抬到台面上方 0.6m → safe（零点跟随台面而非地坪）",
      r3.state === "safe", `clearance=${f3(r3.clearance)} state=${r3.state}`);
    f3f.dispose();

    // ★ 加了站台后，地面碰撞检测依然有效：把手臂折下来必须能触发 hit
    const { root: rr, joints: jj, applyJoints } = buildRobot(FALLBACK_DH, "floor");
    rr.position.y = PLATFORM_H;
    const s2 = new THREE.Scene(); s2.add(rr);
    const fence = new SafetyFence(new THREE.Scene());
    fence.applyConfig(cfg);
    let worst = Infinity, worstQ = 0;
    for (let deg = -100; deg <= 100; deg += 10) {
      applyJoints([0, deg, 0, 0, 0, 0]);
      s2.updateMatrixWorld(true);
      const res = fence.update(rr, jj[0], 1.0, PLATFORM_H);
      if (res.clearance < worst) { worst = res.clearance; worstQ = deg; }
    }
    ok("★ 折臂向台面下方摆时能触发 hit（站台没有把地面检测架空）",
      worst < 0.03, `最低 clearance=${f3(worst)} @J2=${worstQ}°`);
    ok("零位站立时不会误报 hit（台面高度不算进离地距离）",
      (() => {
        applyJoints([0, 0, 0, 0, 0, 0]);
        s2.updateMatrixWorld(true);
        const res = fence.update(rr, jj[0], 1.0, PLATFORM_H);
        return res.state === "safe";
      })());
    fence.dispose();
  }
}

console.log("\n== 5. ★ 地面报警垫改铺到台面上（否则会被站台整块盖住） ==");
{
  const cfg = {
    version: 1, enabled: true,
    zones: [{
      id: "z1", name: "主工作区", enabled: true, shape: "rect",
      center: { x: 0, z: 0 }, half: { x: 0.82, z: 0.82 }, radius: 0.9,
      height: 1.2, walls: true, posts: { enabled: true, size: 0.1, color: "#f2c500" },
      thresholds: { basis: "halfwidth", warn: 0.3, danger: 0.1, fixed_mm: 300 },
    }],
    blink: { hz: 4, min: 0.18, max: 0.63 },
    alarm: { banner: true, chip: true, banner_min: "danger", sound: false },
    walls: true, ground: { enabled: true, warn_mm: 150, danger_mm: 80, hit_mm: 30 },
    overlay: { bbox: false },
  };
  const sc = new THREE.Scene();
  const f = new SafetyFence(sc);
  f.applyConfig(cfg);

  // 默认（不设台面）：垫子铺在房间地坪，且铺满区域多边形(1.64m)
  const d0 = new THREE.Box3().setFromObject(f.groundMesh);
  ok("默认：垫子铺在地坪上(y≈0.004)",
    Math.abs(f.groundMesh.position.y - 0.004) < 0.002, `y=${f3(f.groundMesh.position.y)}`);
  ok("默认：垫子跟随区域多边形(1.64m)",
    Math.abs((d0.max.x - d0.min.x) - 1.64) < 0.02, `w=${f3(d0.max.x - d0.min.x)}`);

  // 设了台面：垫子抬到台面顶、尺寸改成台面大小（与 manager 传参一致 → 铺满整个台面）
  f.setPadPlane({ y: PLATFORM_H + 0.004, size: PLATFORM_TOP_SIZE });
  ok("★ 设台面后：垫子抬到台面顶(y = PLATFORM_H + 0.004)",
    Math.abs(f.groundMesh.position.y - (PLATFORM_H + 0.004)) < 0.002,
    `y=${f3(f.groundMesh.position.y)}`);
  const d1 = new THREE.Box3().setFromObject(f.groundMesh);
  ok("★ 设台面后：垫子尺寸 = 台面尺寸（铺满整个拉丝台面、不悬出台沿）",
    Math.abs((d1.max.x - d1.min.x) - PLATFORM_TOP_SIZE) < 0.01 &&
    Math.abs((d1.max.z - d1.min.z) - PLATFORM_TOP_SIZE) < 0.01,
    `${f3(d1.max.x - d1.min.x)}×${f3(d1.max.z - d1.min.z)} vs ${PLATFORM_TOP_SIZE}`);
  ok("★ 设台面后：垫子完全落在台面范围内（不超出台面边界）",
    d1.min.x >= -(PLATFORM_TOP_SIZE / 2) - 0.01 && d1.max.x <= PLATFORM_TOP_SIZE / 2 + 0.01,
    `x=[${f3(d1.min.x)}, ${f3(d1.max.x)}]`);
  ok("描边与垫面同高（不出现描边浮空）",
    Math.abs(f.groundEdge.position.y - (f.groundMesh.position.y + 0.001)) < 1e-6,
    `edge=${f3(f.groundEdge.position.y)}`);

  // 台面设置必须"粘住"：之后任何一次热更新配置都不能把它拽回地坪
  f.applyConfig(cfg);
  ok("★ 热更新配置后垫子仍在台面上（设置不会被重置）",
    Math.abs(f.groundMesh.position.y - (PLATFORM_H + 0.004)) < 0.002,
    `y=${f3(f.groundMesh.position.y)}`);
  ok("四态着色对象在重建后依然可用（groundMat/edgeMat 未丢）",
    !!f.groundMat && !!f.groundEdgeMat);

  // 关掉地面检测 → 不建垫子；padPlane 不影响这个开关
  const cfgOff = JSON.parse(JSON.stringify(cfg));
  cfgOff.ground.enabled = false;
  f.applyConfig(cfgOff);
  ok("关掉地面检测后不建垫子", !f.groundMesh);

  // 传 null 恢复默认：重新开检测应回到地坪
  f.setPadPlane(null);
  f.applyConfig(cfg);
  ok("setPadPlane(null) 后恢复铺在地坪上",
    Math.abs(f.groundMesh.position.y - 0.004) < 0.002, `y=${f3(f.groundMesh.position.y)}`);
  f.dispose();
}

console.log("\n== 6. 常量自洽 ==");
{
  ok("PLATFORM_H 是一点点抬高（0.05~0.30m）",
    PLATFORM_H > 0.05 && PLATFORM_H < 0.30, String(PLATFORM_H));
  ok("PLATFORM_SIZE 放得下机器人底座(≥1.0m)",
    PLATFORM_SIZE >= 1.0, String(PLATFORM_SIZE));
  ok("PLATFORM_SIZE 小于工作区地坪",
    PLATFORM_SIZE < FLOOR_SIZE, `${PLATFORM_SIZE} < ${FLOOR_SIZE}`);
}

console.log("\n== 7. ★ D5′ 立足面高度：前端常量与后端配置必须是同一个数 ==");
{
  // A1 后端独立评估地面净空时读的是 `config/robot.yaml` 的 `safety.platform_height_mm`，
  // 而 3D 画面里机器人"站在多高"用的是这里的 PLATFORM_H。**两个数一旦分叉**：
  // 界面显示"离台面还有 190mm"、后端却按 180mm 判 —— 报警与画面互相打脸（§5.5 铁律④）。
  const yaml = readFileSync(join(root, "..", "config", "robot.yaml"), "utf8");
  const m = /^  platform_height_mm:\s*([-\d.]+)/m.exec(yaml);
  ok("config/robot.yaml 里有 safety.platform_height_mm（后端不再靠猜）", !!m,
     m ? "" : "配置项缺失");
  if (m) {
    const cfgMm = Number(m[1]);
    ok(`platform_height_mm(${cfgMm}) == PLATFORM_H×1000(${f3(PLATFORM_H * 1000)})`,
       Math.abs(cfgMm - PLATFORM_H * 1000) < 0.001, `${cfgMm} vs ${PLATFORM_H * 1000}`);
    // 后端默认兜底值也必须与配置一致（配置读坏时会退回它，不能是另一个数）
    const be = readFileSync(join(root, "..", "backend", "app", "services", "safety_eval.py"), "utf8");
    const fb = /platform_height_mm",\s*([-\d.]+)\)/.exec(be);
    ok("后端读取的**兜底默认值**与配置同一个数（配置坏了也不分叉）",
       !!fb && Math.abs(Number(fb[1]) - cfgMm) < 0.001, fb ? fb[1] : "没读到默认值");
    ok("后端确实从配置读立足面高度（不是硬编码 0.18）",
       /platform_height_mm/.test(be) && !/refFloorY\s*=\s*0\.18/.test(be));
  }
}

console.log(`\n结果: ${pass} PASS / ${fail} FAIL`);
process.exit(fail ? 1 : 0);
