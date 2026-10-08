// =====================================================================
// 残影（Ghost / 预演分身）：用**程序化模型**再建一份半透明机器人，
// 用"淡淡的分身"预演下一个点位 —— 实机还在动，残影已经站在终点位上，
// 操作者一眼就能判断"下一步它要摆成什么样"。
//
// ★★ 铁律：残影**绝不能 clone 官方 GLB 的单 mesh**（会整体放大 1000 倍，本项目已踩过）。
//    所以残影一律基于 robotModel.buildRobot 的程序化本体；官方数模残影是另一条路
//    （二次 loadAsync 独立加载），本文件不涉及。
//
// ★ 残影**默认不参与**围栏 / 地面碰撞计算：manager 的 getActiveRoot() / getGroundRoot()
//   只认实体机（robot / official），残影是独立 root，天然被排除。
//   → 残影可以"站在危险区里"预演而不影响实机的实时读数。
//
// ★ 但**下发判据**恰恰要用它：manager 的 evalSafety() 在残影可见时会把残影摆到
//   **目标位姿**再量一次围栏/地面（withTarget），"要去的地方会不会撞"就由它回答。
//   残影目标位姿危险/碰撞时整体泛红（setAlarm），一眼看出"这个点位不能去"。
// =====================================================================
import * as THREE from "three";
import { buildRobot } from "../robot/robotModel.js";
import { buildEndEffector } from "./endEffector.js";

export const GHOST_OPACITY = 0.26;      // 半透明程度（越小越"虚"）
export const GHOST_COLOR = 0x2f9bff;    // 残影 A：淡蓝，和实体机的灰白明显区分
// ★ 残影 B（对比用）：淡橙。两个残影同屏时必须**一眼分开**，否则看不出哪个是哪个。
export const GHOST_COLOR_B = 0xff9a3d;
export const GHOST_ALARM_COLOR = 0xff3b30; // 目标位姿危险/碰撞时的报警色（整体泛红）
const GHOST_K = 10;                     // 残影自身的一阶收敛速率（比实机略慢，"飘"过去更易读）

/**
 * 建一个残影。参数与 buildRobot 一致（同一套 DH / 安装方式 / 台面抬升）。
 * @returns {{root, setPose(qDeg):void, step(dt):void, setVisible(v):void,
 *            setAlarm(on):void, withTarget(fn):any, visible:boolean, alarmed:boolean,
 *            dispose():void, meshCount:number}}
 */

export function createGhost(dh, mounting = "floor", liftY = 0, colorHex = GHOST_COLOR) {
  const model = buildRobot(dh, mounting);

  // ★ 需求：残影也要显示 J6 上的那个末端工具（吸盘 + 两块矩形立板），并跟随 J6 一起动。
  //   ★★ 复用**同一个** buildEndEffector()（官方模型也用它）—— 从此两边外观不可能不一致；
  //      以前残影自己手写了一份简化版，才出现"残影看不到 J6 工具"。
  //   ★ 尺度：该工具是按官方那条 **root.scale = 0.001** 的链设计的（几何值是"近似 mm"），
  //     而程序化模型本身是"米"（robotModel 里 j.d / SCALE，SCALE=1000）→ 这里必须再乘 0.001，
  //     否则整套工具会被放大 1000 倍（残影文件头的铁律同样适用于末端工具）。
  //   ★ 挂载点用 model.flangeNode（按 DH 的 d6 算出的法兰面）；**不要硬编码 z**，
  //     往腕部筒身里偏一点，工具就整块埋进去看不见了。
  //   必须在下面"统一染成半透明蓝 + 收集 tint"之前挂上去，末端工具才会一起泛红。
  try {
    if (model.flangeNode) {
      const ee = buildEndEffector();
      ee.group.scale.setScalar(0.001);   // 对齐官方 0.001 缩放链（单位：米）
      model.flangeNode.add(ee.group);
    }
  } catch (e) { /* 末端工具缺失不影响残影本体 */ }

  const tint = [];          // 参与报警着色的材质（基色统一 GHOST_COLOR）
  let meshCount = 0;
  model.root.traverse((o) => {
    if (!o.isMesh) return;
    const mats = Array.isArray(o.material) ? o.material : [o.material];
    const ghostMats = mats.map((m) => {
      const c = m.clone();
      c.transparent = true;
      c.opacity = GHOST_OPACITY;
      c.depthWrite = false;              // ★ 不写深度：不遮挡实体机，也不会被自己穿帮
      if (c.color) c.color = new THREE.Color(colorHex);
      if ("metalness" in c) c.metalness = 0.0;
      if ("roughness" in c) c.roughness = 1.0;
      if ("emissive" in c) { c.emissive = new THREE.Color(colorHex); c.emissiveIntensity = 0.35; }
      tint.push(c);
      return c;
    });
    o.material = Array.isArray(o.material) ? ghostMats : ghostMats[0];
    o.castShadow = false;
    o.receiveShadow = false;
    o.renderOrder = 999;                 // 最后渲染，压在实体机之上
    o.raycast = () => {};                // 不可拾取，避免误选
    meshCount += 1;
  });

  model.root.name = "ghost-root";
  model.root.position.y = liftY;
  model.root.visible = false;

  const target = [0, 0, 0, 0, 0, 0];
  const current = [0, 0, 0, 0, 0, 0];
  let primed = false;

  /** 把残影摆到指定关节角（度）。首次调用直接落位，之后平滑"飘"过去。 */
  function setPose(qDeg) {
    for (let i = 0; i < 6; i++) {
      target[i] = qDeg && qDeg[i] != null ? Number(qDeg[i]) : 0;
    }
    if (!primed) {
      for (let i = 0; i < 6; i++) current[i] = target[i];
      primed = true;
      model.applyJoints(current);
    }
    model.root.visible = true;
  }

  /** 每帧推进（由 manager 的渲染循环调用）。 */
  function step(dt) {
    if (!primed || !model.root.visible) return;
    const k = 1 - Math.exp(-dt * GHOST_K);
    let moved = false;
    for (let i = 0; i < 6; i++) {
      const d = target[i] - current[i];
      if (Math.abs(d) < 1e-4) { current[i] = target[i]; continue; }
      current[i] += d * k;
      moved = true;
    }
    if (moved) model.applyJoints(current);
  }

  function setVisible(v) { model.root.visible = !!v; }

  /**
   * 报警着色：目标位姿危险/碰撞时整体泛红，解除时还原淡蓝。
   * ★ 只改颜色/自发光，**不动** transparent / opacity / depthWrite ——
   *   残影的"半透明、不遮挡实体机"是硬约束（见 verify_ghost.mjs 的断言）。
   */
  let alarmed = false;
  function setAlarm(on) {
    const v = !!on;
    if (v === alarmed) return;
    alarmed = v;
    const hex = v ? GHOST_ALARM_COLOR : GHOST_COLOR;
    for (const m of tint) {
      if (m.color) m.color.setHex(hex);
      if (m.emissive) m.emissive.setHex(hex);
      if (m.emissive && m.emissiveIntensity !== undefined) m.emissiveIntensity = v ? 0.85 : 0.35;
    }
  }

  /**
   * 用**目标姿态**（而不是动画中的当前姿态）执行一次测量。
   * ★ 预演判定必须看终点，不能看"飘到一半"的位置；测完立刻还原动画姿态，
   *   视觉上没有任何跳变（与 manager.getTcp() 的临时摆姿是同一手法）。
   * @param {(root:THREE.Object3D, joints:THREE.Object3D[]) => any} fn
   */
  function withTarget(fn) {
    const prev = current.slice();
    model.applyJoints(target);
    try { return fn(model.root, model.joints); }
    finally { model.applyJoints(prev); }
  }

  /** 释放（切机型需要重建时用）。 */
  function dispose() {
    model.root.traverse((o) => {
      if (!o.isMesh) return;
      const mats = Array.isArray(o.material) ? o.material : [o.material];
      mats.forEach((m) => m && m.dispose && m.dispose());
    });
    if (model.root.parent) model.root.parent.remove(model.root);
  }

  return {
    root: model.root,
    setPose, step, setVisible, dispose, setAlarm, withTarget,
    get visible() { return model.root.visible; },
    get alarmed() { return alarmed; },
    meshCount,
  };
}
