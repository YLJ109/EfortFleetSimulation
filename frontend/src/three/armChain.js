// =====================================================================
// 华沿 ELFIN 关节链建模 —— 按官方 URDF/xacro 的口径直接搭，不转 DH。
//
// ★ 为什么不转 DH（与 robotModel.js 的埃夫特那条链最大的差别）：
//   官方只给关节链：  T_i = Translate(xyz) · Rz(yaw)·Ry(pitch)·Rx(roll) · Rz(θ_i)
//   （xyz/rpy 单位 m/rad，抄自 vendor/elfin_model/urdf/*.urdf.xacro 原文）。
//   硬套标准 DH 要引入一次换算，而换算错了没人看得出来 —— 手臂画错姿态
//   在机房里就是安全隐患。所以这里**原样实现那条链**。
//
// ★ rpy 的顺序是坑，必须写清楚：URDF 规范里 rpy="roll pitch yaw"（第一个数是 roll），
//   旋转矩阵 R = Rz(yaw)·Ry(pitch)·Rx(roll)。
//   huayan_elfin.yaml 早年注释里写成 (yaw,pitch,roll)，是**抄错的注释**，已改。
//   两种读法的判据现成：URDF 序在零位算出法兰 1020mm / 末端 1176.5mm，
//   正好等于官方 README 机型表里 E05 的「站立高度」1176.5mm；
//   反过来按 (yaw,pitch,roll) 读，末端会躺到 220mm 高 —— 一眼就崩。
//   这条判据固定在 tools/verify_armchain.mjs 里，六个机型逐一比对。
//
// ★ 坐标系：根节点绕 X 转 -90°，把 URDF 的 +Z(基座竖直轴) 映射成 Three 的 +Y(上)，
//   与 robotModel.js / 官方 GLB 那条链一致，这样 12 台混排时两套身体共用同一套摆放代码。
//   单位：配置就是米，场景也是米，**没有** GLB 那个 0.001 缩放。
//
// ★ 只读：这里只做正运动学（把真机角度画成姿态）。没有逆解、没有下发、没有写路径。
// =====================================================================
import * as THREE from "three";
import { SKIN_BODY, SKIN_HARD, SKIN_JOINT, SKIN_METAL, skinMat } from "./materials.js";

const deg2rad = (d) => (d * Math.PI) / 180;

/** 关节鼓半径：肩部粗、腕部细（协作臂的真实比例，也避免 12 台同时渲染太重的几何）。 */
const DRUM_R = [0.082, 0.078, 0.068, 0.062, 0.055, 0.050];
const DRUM_T = [0.060, 0.060, 0.052, 0.046, 0.044, 0.040];

/** 沿自身 +Z 的圆柱（半径 r、高 h，近端面在 z0）。three 的 Cylinder 中心在原点，故 +h/2。 */
function cylZ(r, h, mat, z0 = 0) {
  const m = new THREE.Mesh(new THREE.CylinderGeometry(r, r, h, 24), mat);
  m.rotation.x = Math.PI / 2;
  m.position.z = z0 + h / 2;
  return m;
}

/** 从原点指向 to 的连杆（胶囊状）；to 用**本关节坐标系**表达，长度不足则不画。 */
function linkTo(to, r, mat) {
  const len = Math.hypot(to[0], to[1], to[2]);
  if (!(len > 0.01)) return null;
  const m = new THREE.Mesh(new THREE.CylinderGeometry(r, r, len, 20), mat);
  m.name = "link-bar";            // 回归测试按这个名字找它（tools/verify_armchain.mjs 第 7 节）
  m.position.set(to[0] / 2, to[1] / 2, to[2] / 2);
  // ★ CylinderGeometry 的轴是**局部 +Y**，不是 +Z（cylZ 那个助手靠 rotation.x=90° 才变成 Z 轴）。
  //   这里以前把 +Z 旋到连杆方向，等于让圆柱**横着躺**：杆子从关节原点伸向侧面，
  //   永远够不到下一个关节 —— 画出来就是"脱骨"，六节鼓各自悬空。
  m.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0),
    new THREE.Vector3(to[0] / len, to[1] / len, to[2] / len));
  return m;
}

/**
 * URDF 固定角 → 四元数：R = Rz(yaw)·Ry(pitch)·Rx(roll)，rpy = [roll, pitch, yaw]。
 * ★ 刻意不用 THREE.Euler：它的默认顺序是 XYZ，语义上是"绕**动**轴"转，
 *   和 URDF 的"绕**固定**轴 yaw→pitch→roll"不同。矩阵显式相乘没有歧义。
 */
function urdfQuaternion(rpy) {
  const roll = Number(rpy && rpy[0]) || 0;
  const pitch = Number(rpy && rpy[1]) || 0;
  const yaw = Number(rpy && rpy[2]) || 0;
  const m = new THREE.Matrix4().makeRotationZ(yaw)
    .multiply(new THREE.Matrix4().makeRotationY(pitch))
    .multiply(new THREE.Matrix4().makeRotationX(roll));
  return new THREE.Quaternion().setFromRotationMatrix(m);
}

/**
 * 造一台 ELFIN（按机型档案给的关节链）。
 * @param {object} urdf 后端 /api/fleet 的 kinematics.<model>.urdf：
 *   { joints: [{name, xyz[m], rpy[rad], lower, upper}], end_fixed: {xyz, rpy} }
 * @param {object} [mats] 共享材质集 {body, joint, hard, metal}；12 台共用一份时由调用方传入
 *   （skinMat 每次调用都新建材质，各建各的会白涨 48 个材质）。
 * @returns {{root, joints, flangeNode, applyJoints, getTcp, links}}
 */
export function buildElfChain(urdf, mats, meshes) {
  const J = urdf && urdf.joints;
  if (!Array.isArray(J) || J.length !== 6) {
    throw new Error("ELFIN 关节链需要 6 轴，实际拿到 " + (J ? J.length : "无") + " —— 检查 /api/fleet 的 kinematics.urdf");
  }
  const M = mats || {
    body: skinMat(SKIN_BODY, 0.15, 0.42),
    joint: skinMat(SKIN_JOINT, 0.45, 0.38),
    hard: skinMat(SKIN_HARD, 0.55, 0.40),
    metal: skinMat(SKIN_METAL, 0.80, 0.28),
  };

  const root = new THREE.Group();
  root.name = "elfin-root";
  root.rotation.x = -Math.PI / 2;   // URDF +Z(上) → Three +Y(上)

  // ---- 真实网格（可选）：厂商 STL 转成的几何，按 URDF 的 link 挂 ----
  // 给了 meshes 就不再画胶囊体 —— 胶囊是给"还没拿到官方网格"准备的兜底画法，
  // 两套同时存在会在同一根连杆上叠出双层表面（z-fighting，转角度时闪成一片）。
  const realMesh = meshes && meshes.links && meshes.links.length === 6;
  if (meshes && meshes.base) {
    const m = new THREE.Mesh(meshes.base, M.body);
    m.name = "elfin-base";
    root.add(m);
  }

  // ---- 固定底座：安装法兰盘 + 立柱，立柱顶到 J1 轴 ----
  const zJ1 = Number(J[0].xyz && J[0].xyz[2]) || 0;
  root.add(cylZ(DRUM_R[0] + 0.016, 0.022, M.hard, 0));
  if (zJ1 > 0.03) root.add(cylZ(DRUM_R[0] - 0.006, zJ1 - 0.022, M.body, 0.022));

  const joints = [];
  const links = [];
  let parent = root;
  for (let i = 0; i < 6; i++) {
    const xyz = [Number(J[i].xyz?.[0]) || 0, Number(J[i].xyz?.[1]) || 0, Number(J[i].xyz?.[2]) || 0];

    // 静态偏置层：先平移 xyz，再转固定角 rpy（父坐标系 → 本关节坐标系）
    const link = new THREE.Group();
    link.name = "link" + (i + 1);
    link.position.set(xyz[0], xyz[1], xyz[2]);
    link.quaternion.copy(urdfQuaternion(J[i].rpy));
    parent.add(link);

    // 转动层：关节角 θ 只绕本坐标系 z 轴转（applyJoints 写的就是这里）
    const jg = new THREE.Group();
    jg.name = "J" + (i + 1);
    link.add(jg);

    if (realMesh) {
      const m = new THREE.Mesh(meshes.links[i], i === 0 ? M.joint : M.body);
      m.name = "elfin-link" + (i + 1);
      jg.add(m);
    } else {
      jg.add(cylZ(DRUM_R[i], DRUM_T[i], M.joint, -DRUM_T[i] / 2));   // 关节鼓（轴在局部原点）
      const next = J[i + 1] && J[i + 1].xyz;
      const bar = linkTo(next ? [Number(next[0]) || 0, Number(next[1]) || 0, Number(next[2]) || 0]
        : endOffset(urdf), DRUM_R[i] * 0.72, M.body);
      if (bar) jg.add(bar);
    }

    joints.push(jg);
    links.push(link);
    parent = jg;
  }

  // ---- J6 法兰 + 末端（end_fixed：未装工具时的 TCP，官方 elfin_end_joint）----
  const end = urdf.end_fixed || {};
  const flangeNode = new THREE.Group();
  flangeNode.name = "Flange";
  flangeNode.position.set(Number(end.xyz?.[0]) || 0, Number(end.xyz?.[1]) || 0, Number(end.xyz?.[2]) || 0);
  if (end.rpy) flangeNode.quaternion.copy(urdfQuaternion(end.rpy));
  parent.add(flangeNode);
  flangeNode.add(cylZ(DRUM_R[5] - 0.004, 0.016, M.metal, -0.008));  // 铝色法兰面

  /**
   * 应用关节角(度)。
   * 坏帧直接整帧丢弃（返回 false）：把某一轴读成 NaN 时若继续，其余五轴会被瞬移回 0 位，
   * 画面突然"甩臂"比保持上一帧危险得多。
   * ±360° 之外的数值不可能是真机读数（控制器给的是主值区间），一律夹住；
   * 但**不夹到 xacro 限位** —— 本文件只负责如实显示，超限位由 HUD 用
   * kinematics.joint_limits 标红提示，3D 里悄悄折回来等于对操作员撒谎。
   */
  function applyJoints(qDeg) {
    const rad = [];
    for (let i = 0; i < 6; i++) {
      const raw = Number(qDeg && qDeg[i]);
      if (!Number.isFinite(raw)) return false;
      rad.push(deg2rad(Math.max(-360, Math.min(360, raw))));
    }
    for (let i = 0; i < 6; i++) joints[i].rotation.z = rad[i];
    return true;
  }

  function getTcp() {
    root.updateMatrixWorld(true);
    const v = new THREE.Vector3();
    flangeNode.getWorldPosition(v);
    return { x: v.x * 1000, y: v.y * 1000, z: v.z * 1000 };
  }

  // 建好即摆零位：不先应用一次的话，首帧渲染的是"各轴 0 但连杆没对齐"的怪姿态，
  // 地面碰撞检测会在这半秒里误判成穿地（见 robotModel.js 同名处理）。
  applyJoints([0, 0, 0, 0, 0, 0]);

  return { root, joints, flangeNode, applyJoints, getTcp, links };
}

/** 最后一轴没有"下一个关节"时，连杆画到末端偏置处。 */
function endOffset(urdf) {
  const e = (urdf && urdf.end_fixed && urdf.end_fixed.xyz) || [];
  return [Number(e[0]) || 0, Number(e[1]) || 0, Number(e[2]) || 0];
}
