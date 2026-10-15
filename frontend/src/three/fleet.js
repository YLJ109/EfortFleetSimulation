// =====================================================================
// 机房 12 台 —— 三维场景里的"阵列层"（fleet layer）
//
// 与单机层（manager.js 的 robot/official/fence/ghost/sim）的关系：
//   两层**互斥显示**。阵列以原点为中心铺开 4.28m × 9.28m，单机层那台机器人正好
//   站在原点，同时显示会重叠成一团，所以进入机房视图时整层关掉单机层。
//
// ★ 三条不能错的坐标口径：
//   1) 后端 fleet.yaml 的 pos 是**地面坐标**（x=区轴、y=纵深、yaw 绕竖直轴）；
//      Three 里地面是 XZ 平面、上方向是 +Y，映射为 x→x、y→−z、yaw→rotation.y。
//      （与 SimMonitor.vue 里 dh.x=three.x / dh.y=−three.z 同一套约定。）
//   2) 每台站姿由一个**工位组**承载（position + rotation.y），机器人本体是它的子节点。
//      本体自带 root.rotation.x=-90°（DH/URDF 的 +Z→Three +Y），若把偏航直接写到
//      本体上，两个欧拉序叠在一起只有在 yaw=0 时才碰巧对 —— 换朝向就全错。
//   3) 围栏的几何与判定都在**世界 XZ**里做（SafetyFence 用 Box3 世界包围盒比半空间），
//      所以围栏不挂进转过的工位组，只挂进没有自身变换的 fleet 根组。
//
// ★ 只读层：这里没有任何下发入口。姿态只进不出。
// =====================================================================
import * as THREE from "three";
import { SafetyFence, STATE_RANK } from "./safety.js";

const deg2rad = (d) => (d * Math.PI) / 180;

/**
 * HUD 刻度盘：内圈实线环 + 外圈虚线刻度（参考图里机柜脚下那圈青色圆盘）。
 * 颜色在 update() 里跟**这一台**的围栏等级走 —— 扫一眼地面就知道哪台出事，
 * 不用逐张卡片读字；12 台同框时这比任何文字都快。
 */
const HUD_COLOR = { safe: 0x48b7af, warn: 0xf0a840, danger: 0xff4d5e, hit: 0xff4d5e };

function buildHudRing() {
  const g = new THREE.Group();
  g.name = "hud-ring";
  const ringMat = new THREE.MeshBasicMaterial({
    color: HUD_COLOR.safe, transparent: true, opacity: 0.42, side: THREE.DoubleSide,
  });
  const ring = new THREE.Mesh(new THREE.RingGeometry(0.6, 0.68, 48), ringMat);
  ring.rotation.x = -Math.PI / 2;
  ring.position.y = 0.012;
  g.add(ring);

  const pts = new THREE.EllipseCurve(0, 0, 0.8, 0.8, 0, Math.PI * 2, false, 0)
    .getPoints(72).map((p) => new THREE.Vector3(p.x, 0.012, p.y));
  const lineGeo = new THREE.BufferGeometry().setFromPoints(pts);
  const lineMat = new THREE.LineDashedMaterial({
    color: HUD_COLOR.safe, transparent: true, opacity: 0.8, dashSize: 0.055, gapSize: 0.085,
  });
  const line = new THREE.Line(lineGeo, lineMat);
  line.computeLineDistances();
  g.add(line);

  let cur = HUD_COLOR.safe;
  return {
    group: g,
    setColor(hex) {
      if (hex === cur) return;
      cur = hex;
      ringMat.color.set(hex);
      lineMat.color.set(hex);
    },
    dispose() {
      ring.geometry.dispose(); ringMat.dispose();
      lineGeo.dispose(); lineMat.dispose();
    },
  };
}

/**
 * 残影（ghost）配色：目标姿态安全与否，直接涂在残影自己身上。
 * 与 HUD 刻度盘同一套色，眼睛不用换字典。
 */
const GHOST_TINT = { safe: 0x48b7af, warn: 0xf0a840, danger: 0xff4d5e, hit: 0xff2020 };

/**
 * 一阶指数收敛速率(1/s)，与 manager.js 单机层的 SMOOTH_K 同值。
 * 两处刻意保持一致：同一个操作员在单机视图和机房视图里看到的"跟手程度"必须一样，
 * 否则他会以为机房视图卡了。
 */
const SMOOTH_K = 14;

/** 主值区间 (−180, 180]：关节角是周期量，写进 rotation.z 前归一下，数值不会越夹越远。 */
function wrap180(a) {
  return ((a + 180) % 360 + 360) % 360 - 180;
}

/** 深拷贝一个区域模板（绝不能改到 store 里那份配置，HUD 的滑块还在用它）。 */
function cloneZone(z) {
  return JSON.parse(JSON.stringify(z));
}

/**
 * 工位围栏区域：把区域模板挪到这台机器的地面上。
 * ★ 半边长取**阵列间距的一半**（fleet.yaml 的 cell_pitch_m/2），不取 safety.json 的 half：
 *   12 台围栏要正好铺满整块阵列，间距是这张地图的权威；两者不一致时
 *   tools/verify_fleet.mjs 会报错（而不是画面里围栏互相重叠或留出缝）。
 */
function zoneForStation(base, st, half) {
  const z = cloneZone(base);
  z.id = st.id;
  z.name = st.name || st.id;
  z.center = { x: st.pos.x, z: -st.pos.y };
  z.half = { x: half, z: half };
  // ★ 2.5D 俯视等于"从上往下看穿四面半透明墙"：单机视图里 .11 很淡，这里四面一叠
  //   就成了 12 块发绿的方块，机房地面被糊成一片。安全档压到只剩边线感；
  //   报警档（warn/danger/hit）**不压** —— 出事那台必须还是全场最刺眼的。
  if (z.opacity) z.opacity.safe = 0.04;
  // ★ 围栏柱子整根去掉（用户口径）：12 × 4 根黄柱在等轴视角里就是一排排电线杆，
  //   把机器人全挡在后面。围栏本身（判定 + 墙 + 地面垫）一根没动，只是不再画柱子。
  if (z.posts) z.posts.enabled = false;
  return z;
}

function cfgForStation(cfg, st, half) {
  const zones = [zoneForStation((cfg.zones && cfg.zones[0]) || {}, st, half)];
  const out = {
    ...cfg,
    zones,
    // 12 个包围盒线框叠在一起只会糊成一片，机房视图不开这个叠加层
    overlay: { bbox: false },
    // ★ 地面报警垫**只把安全档调成全透明**，不整块关掉。
    //   试过 enabled:false，画面确实干净了，但地面碰撞检查（机械臂低过立足面）
    //   是跟着这块垫子走的 —— 关垫子等于把那一半判据一起关掉，
    //   tools/verify_fleet.mjs 第 4 节当场就红了。判据不能为好看让路。
    //   现在：健康的机位脚下是空的（状态由 HUD 刻度盘 + 卡片色条说），
    //   一旦逼近/碰撞，这块垫子自己会亮起来 —— 反而比 12 张常亮地毯更醒目。
    ground: cfg.ground
      ? { ...cfg.ground, opacity: { ...(cfg.ground.opacity || {}), safe: 0 } }
      : cfg.ground,
  };
  return out;
}

/**
 * 建一整套机房阵列。
 * @param {object} opt
 *   opt.scene       THREE.Scene（单例场景）
 *   opt.robots      /api/fleet 的 robots[]，每项含 id/name/zone/model/pos{x,y,yaw}
 *   opt.safetyCfg   归一化后的安全配置（颜色/阈值/墙高沿用单机那套）
 *   opt.bodies      { [model]: { root: THREE.Object3D, offsets: number[6] } }
 *                   每个型号**一个模板**，12 台按 clone() 复制（几何与材质共享）
 * @returns {{group, stations, unbuilt, setPoses, step, update, setVisible, dispose}}
 */
export function buildFleet(opt) {
  const { scene, robots = [], safetyCfg, bodies = {} } = opt || {};
  const layout = (opt && opt.layout) || {};
  const pitch = Number(layout.cell_pitch_m) || 1.64;
  const half = pitch / 2;

  const group = new THREE.Group();
  group.name = "fleet";
  scene.add(group);

  const stations = [];
  const unbuilt = [];

  robots.forEach((r) => {
    const body = bodies[r.model];
    if (!body || !body.root) {
      unbuilt.push({ id: r.id, model: r.model, reason: "没有这个型号的模型档案" });
      return;
    }
    if (!r.pos || !Number.isFinite(r.pos.x) || !Number.isFinite(r.pos.y)) {
      unbuilt.push({ id: r.id, model: r.model, reason: "登记里没有地面坐标 pos" });
      return;
    }

    // ---- 工位组：只负责"这台机器站在哪儿、朝哪儿" ----
    const st = new THREE.Group();
    st.name = "station-" + r.id;
    st.position.set(r.pos.x, 0, -r.pos.y);
    st.rotation.y = deg2rad(Number(r.pos.yaw) || 0);
    group.add(st);

    // ---- 本体：模板的副本。clone() 共享 geometry/material，12 台不重复上传显存 ----
    const inst = body.root.clone();
    inst.position.set(0, 0, 0);   // 模板自带的抬升（单机层抬到站台面）在这里作废：机房直接落地坪
    inst.visible = true;
    st.add(inst);

    const joints = [];
    for (let i = 1; i <= 6; i++) {
      const jg = inst.getObjectByName("J" + i);
      if (!jg) {
        unbuilt.push({ id: r.id, model: r.model, reason: "模型缺少 J" + i + " 关节节点" });
        st.removeFromParent();
        return;
      }
      joints.push(jg);
    }

    // 围栏配置还没到（安全配置是另一条异步链路）→ 这一台**照样建出来**，只是暂不
    // 评估围栏。以前这里直接 cfgForStation(null) 抛异常，结果是整层 12 台一起消失、
    // 大屏变成一片黑 —— 少一个可选的评估，不该让主角整个不见。
    let stFence = null;
    if (safetyCfg) {
      stFence = new SafetyFence(group);   // 世界 XZ 判定 → 挂在无变换的 fleet 根组下
      stFence.applyConfig(cfgForStation(safetyCfg, r, half));
    }
    // ★ 不调 setPadPlane：报警垫默认铺"第一个启用区域的多边形"，而那个区域已经
    //   平移到本工位（center 生效）。setPadPlane 给的是**以原点为中心**的方形台面，
    //   12 台会全部叠到机房正中间。

    const hud = buildHudRing();
    st.add(hud.group);

    stations.push({
      id: r.id, name: r.name || r.id, zone: r.zone, brand: r.brand,
      model: r.model, order: r.order, host: r.host, port: r.port,
      group: st, root: inst, joints, offsets: body.offsets || [0, 0, 0, 0, 0, 0],
      // 真网格版身体：只在**被选中那台**上启用（见 setReal 的性能说明）
      bodyReal: body.real ? body.real.root : null, offsetsReal: (body.real && body.real.offsets) || null,
      instReal: null, jointsReal: null, usingReal: false,
      fence: stFence, hud,
      // dirty=true 起步：第一帧必须真算一次，否则颜色与净空全是初始占位值
      row: null, dirty: true,
      qTarget: [0, 0, 0, 0, 0, 0],
      qCurrent: [0, 0, 0, 0, 0, 0],
      ready: false,
      state: null,
    });
  });

  const byId = new Map(stations.map((s) => [s.id, s]));

  // ---- 深色地坪 + 钢蓝网格：机房大屏不要实验室那间白屋子 ----
  // 进机房时 manager 会把 lab 整间隐藏，这里补一块"数字孪生空腔"的地：
  // 底色与 index.html --idc-bg 同值，网格用 --idc-line 那档钢蓝。
  const disposables = [];
  {
    let x0 = Infinity, x1 = -Infinity, z0 = Infinity, z1 = -Infinity;
    for (const r of robots) {
      if (!r.pos || !Number.isFinite(r.pos.x) || !Number.isFinite(r.pos.y)) continue;
      x0 = Math.min(x0, r.pos.x); x1 = Math.max(x1, r.pos.x);
      z0 = Math.min(z0, -r.pos.y); z1 = Math.max(z1, -r.pos.y);
    }
    if (x0 <= x1) {
      const pad = 2.4;
      const w = x1 - x0 + pad * 2, d = z1 - z0 + pad * 2;
      const cx = (x0 + x1) / 2, cz = (z0 + z1) / 2;
      const gGeo = new THREE.PlaneGeometry(w, d);
      const gMat = new THREE.MeshStandardMaterial({ color: 0x0b1a2a, roughness: 0.96, metalness: 0 });
      const ground = new THREE.Mesh(gGeo, gMat);
      ground.rotation.x = -Math.PI / 2;
      ground.position.set(cx, -0.02, cz);
      ground.name = "fleet-ground";
      group.add(ground);
      disposables.push(gGeo, gMat);

      const span = Math.max(w, d);
      const grid = new THREE.GridHelper(span, Math.max(8, Math.round(span / 0.82)), 0x305274, 0x16344f);
      grid.position.set(cx, -0.014, cz);
      grid.material.transparent = true;
      grid.material.opacity = 0.55;
      grid.name = "fleet-grid";
      group.add(grid);
      disposables.push(grid.geometry, grid.material);
    }
  }

  /** 写入一帧姿态（角度制）。只更新目标值，实际由 step() 逐帧逼近。 */
  function setPose(id, qDeg) {
    const s = byId.get(id);
    if (!s || !qDeg) return;
    let ok = true;
    for (let i = 0; i < 6; i++) {
      const v = Number(qDeg[i]);
      if (!Number.isFinite(v)) { ok = false; break; }   // 坏帧整帧丢弃，见 armChain 同款理由
      s.qTarget[i] = v;
    }
    if (!ok) return;
    // ★ 目标一变就标脏。第一帧的"直接吸附"路径下面 step() 不会再报 moved
    //   （差值已经是 0），漏标这一句会让新姿态被当成"静止且安全"而跳过评估 ——
    //   那是把折臂撞围栏读成安全，比卡顿严重得多。
    s.dirty = true;
    if (!s.ready) {
      for (let i = 0; i < 6; i++) s.qCurrent[i] = s.qTarget[i];
      s.ready = true;
      write(s, s.qCurrent);
    }
  }

  /** 一次帧批：{ robot_id: [6 角度] }。没点到的机器保持上一帧。 */
  function setPoses(map) {
    if (!map) return;
    for (const id in map) setPose(id, map[id]);
  }

  function write(s, q) {
    const j = s.usingReal ? s.jointsReal : s.joints;
    const off = s.usingReal ? s.offsetsReal : s.offsets;
    for (let i = 0; i < 6; i++) {
      j[i].rotation.z = deg2rad(q[i] + ((off && off[i]) || 0));
    }
  }

  /**
   * 切某一台用不用**真网格身体**。
   * ★ 为什么不是 12 台全用真的：官方 STL 一台约 23.5 万三角面（link6 一个件就 12.9 万），
   *   12 台实测 9~10 FPS —— 大屏卡到没法点，比"看着像胶囊"严重得多。
   *   所以：被选中那台（相机也正推在它面前）用原件，其余用关节链骨架。
   *   要 12 台全原件，得先把 STL 简化（decimate）再上，那是另一件事。
   */
  function setReal(id, on) {
    const s = byId.get(id);
    if (!s || !s.bodyReal) return false;
    if (!!on === s.usingReal) return true;
    if (on) {
      if (!s.instReal) {
        const r = s.bodyReal.clone(true);
        r.position.set(0, 0, 0);
        r.visible = true;
        const js = [];
        for (let i = 1; i <= 6; i++) {
          const g = r.getObjectByName("J" + i);
          if (!g) return false;
          js.push(g);
        }
        s.instReal = r; s.jointsReal = js;
        s.group.add(r);
      }
      write(s, s.qCurrent);          // 先对齐当前姿态，避免"一选中就跳回零位"
      s.inst.visible = false;
      s.instReal.visible = true;
      s.usingReal = true;
    } else {
      if (s.instReal) s.instReal.visible = false;
      s.inst.visible = true;
      s.usingReal = false;
    }
    s.dirty = true;                  // 身体换了，包围盒必须重算
    return true;
  }

  /** 逐帧推进平滑。角度按**最短路径**逼近：真机跨过 ±180° 时不能倒转一整圈。 */
  function step(dt) {
    const k = 1 - Math.exp(-dt * SMOOTH_K);
    for (const s of stations) {
      if (!s.ready) continue;
      let moved = false;
      for (let i = 0; i < 6; i++) {
        let d = s.qTarget[i] - s.qCurrent[i];
        if (d > 180) d -= 360; else if (d < -180) d += 360;
        if (Math.abs(d) < 1e-4) { s.qCurrent[i] = s.qTarget[i]; continue; }
        s.qCurrent[i] += d * k;
        moved = true;
      }
      if (moved) { write(s, s.qCurrent.map(wrap180)); s.dirty = true; }
    }
  }

  /**
   * 每帧评估 12 台的安全状态（围栏逐面墙变色 + 报警垫）。
   * 与单机层同一个判据来源：SafetyFence.update()，只是量的是**这一台**的身体。
   * @param {number} tSec 秒（闪烁相位）
   * @returns {{worst: object, list: object[]}} list 里每台一条，worst 取最严重
   */
  function update(tSec) {
    let worst = null;
    for (const s of stations) {
      if (!s.row) {
        s.row = { id: s.id, name: s.name, zone: s.zone, brand: s.brand,
          state: "safe", ratio: 0, clearance: Infinity, zoneName: "" };
      }
      // ★ 静止且安全的工位直接复用上一行结果，不重算包围盒。
      //   dirty 由 step()（姿态真的动了）和 ghostEval()（残影刷过墙面）打上。
      //   safe 档不参与闪烁，只有报警态才需要每帧拿 tSec 刷相位 —— 所以省掉的
      //   这些帧在画面上看不出任何区别。
      if (!s.dirty && s.row.state === "safe") continue;
      s.dirty = false;
      // 地面检测取 J1 以上子树（与单机层同口径）：底座本就贴地，整模型会永远"碰撞"。
      // 机房里机器人直接站地坪 → refFloorY 取 0（单机层那边是站台台面高度）。
      const r = s.fence
        ? s.fence.update(s.root, s.joints[0], tSec, 0)
        : { state: "safe", ratio: 0, clearance: Infinity, zoneName: "" };
      s.state = r;
      s.row.state = r.state;
      s.row.ratio = r.ratio;
      s.row.clearance = r.clearance;
      s.row.zoneName = r.zoneName || "";
      if (s.hud) s.hud.setColor(HUD_COLOR[r.state] || HUD_COLOR.safe);
    }
    for (const s of stations) {
      if (!worst || STATE_RANK[s.row.state] > STATE_RANK[worst.state]) worst = s.row;
    }
    return { worst, list: stations.map((x) => x.row) };
  }

  // ---- 残影（ghost）：目标姿态的半透明副本，回答"要去的地方会不会撞" ----
  // ★ 为什么不用 manager.js 那台单机残影：它站在**原点**（XZ 恒 0、抬升取站台高度），
  //   机房里 12 台各有工位，原点对任何一台都是错的。所以残影按工位建：
  //   clone 这一台自己的本体、挂进这一台自己的工位组（自动继承 x/−z/yaw），
  //   于是"残影落点"这件事根本不需要算。
  let ghost = null;

  function clearGhost() {
    if (!ghost) return;
    ghost.root.removeFromParent();
    // 几何是 clone() 与真身体共享的，**不能** dispose（会把真身体的几何一起释放）；
    // 只有材质是本函数 clone 出来的，只回收这一份。
    ghost.mats.forEach((m) => { try { m.dispose(); } catch (e) { /* 忽略 */ } });
    ghost = null;
  }

  /** 为本体建一份"可涂色"的残影副本。缺关节节点就返回 null（宁可不显示也不显示错的）。 */
  function buildGhost(s) {
    const root = s.root.clone(true);
    root.name = "fleet-ghost-" + s.id;
    root.visible = true;
    const mats = [];
    root.traverse((o) => {
      if (!o.isMesh || !o.material) return;
      const list = Array.isArray(o.material) ? o.material : [o.material];
      const clones = list.map((src) => {
        const m = src.clone();
        m.transparent = true;
        m.opacity = 0.3;
        m.depthWrite = false;                 // 半透明叠层不写深度，否则自身互相遮挡
        m.color = new THREE.Color(GHOST_TINT.safe);
        if (m.emissive) { m.emissive = new THREE.Color(GHOST_TINT.safe); m.emissiveIntensity = 0.5; }
        mats.push(m);
        return m;
      });
      o.material = Array.isArray(o.material) ? clones : clones[0];
    });
    const joints = [];
    for (let i = 1; i <= 6; i++) {
      const jg = root.getObjectByName("J" + i);
      if (!jg) return null;
      joints.push(jg);
    }
    s.group.add(root);
    return { stationId: s.id, root, joints, mats, offsets: s.offsets };
  }

  function tintGhost(state) {
    if (!ghost) return;
    const hex = GHOST_TINT[state] || GHOST_TINT.safe;
    for (const m of ghost.mats) {
      m.color.setHex(hex);
      if (m.emissive) m.emissive.setHex(hex);
    }
  }

  /**
   * 把某一台的目标姿态画成残影，并回答"这个位置安不安全"。
   * @param {string} id 工位 id
   * @param {number[]} qDeg 目标六轴角（度）
   * @returns {{state:string, clearance:number}|null} null = 这台不存在 / 姿态无效
   */
  function ghostEval(id, qDeg) {
    const s = byId.get(id);
    if (!s || !Array.isArray(qDeg) || qDeg.length < 6) return null;
    for (let i = 0; i < 6; i++) if (!Number.isFinite(Number(qDeg[i]))) return null;
    if (!ghost || ghost.stationId !== id) {
      clearGhost();
      ghost = buildGhost(s);
      if (!ghost) return null;
    }
    write(ghost, qDeg.map(Number));
    // 残影评估会顺手把这一台的墙面刷成目标姿态的颜色 —— 标脏，让下一帧用**真实**
    // 姿态重算覆盖回来。不标的话，静止的台子会把残影的颜色一直留在围栏上。
    s.dirty = true;
    // 用**这一台自己的围栏**评估目标姿态。update() 会顺手把围栏刷成目标姿态的颜色，
    // 但下一帧 renderFrame 会用真实姿态重新评估并覆盖 —— 所以围栏始终反映真实状态，
    // 目标姿态只由残影自身的颜色表达，两者不会互相冒充。
    const r = s.fence
      ? s.fence.update(ghost.root, ghost.joints[0], performance.now() / 1000, 0)
      : { state: "safe", clearance: Infinity };
    tintGhost(r.state);
    return { state: r.state, clearance: r.clearance };
  }

  function setVisible(v) { group.visible = !!v; }

  function dispose() {
    clearGhost();
    stations.forEach((s) => {
      try { s.fence && s.fence.dispose(); } catch (e) { /* 忽略 */ }
      try { s.hud && s.hud.dispose(); } catch (e) { /* 忽略 */ }
    });
    disposables.forEach((d) => { try { d.dispose(); } catch (e) { /* 忽略 */ } });
    disposables.length = 0;
    // 本体是模板的 clone，几何/材质与模板共享 —— **不能**在这里 dispose 资源，
    // 否则单机层/下一次重建拿到的是已释放的几何。只从场景里摘掉节点本身。
    group.removeFromParent();
    stations.length = 0;
    byId.clear();
  }

  return {
    group, stations, unbuilt, setPoses, setPose, step, update, setVisible, dispose,
    ghostEval, ghostClear: clearGhost, setReal,
  };
}
