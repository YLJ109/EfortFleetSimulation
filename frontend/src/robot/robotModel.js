// =====================================================================
// ER8-700H 三维模型 (v4) —— 对照现场真机照片复刻。
//
// 外观: 白色机身 + 深灰关节环 + 黑色线缆 + 红色标识条 (与现场一致)
// 结构: 底座立柱 / J1 转塔 / J2 大圆柱肩壳+大臂 / J3 肘壳+肘偏距 /
//       J4 小臂(锥形筒+双侧线缆) / J5 腕壳 / J6 铸造筒+法兰+TCP  全部实体化。
//
// 坐标系约定(与后端 kinematics.py 完全一致):
//   DH 标准: T_i = Rz(theta_i) * Tz(d_i) * Tx(a_i) * Rx(alpha_i)
//   零位约定: J2=0 大臂垂直向上, J3=0 小臂水平向前 (theta_offset 由 yaml 下发)
//   根 Group 绕 X 转 -90°, 使 DH 的 +Z(基座竖直轴) 映射为 Three 的 +Y(上)。
// 单位: 配置 mm, 场景 m (除以 SCALE)。
// =====================================================================
import * as THREE from "three";

const SCALE = 1000;
const deg2rad = (d) => (d * Math.PI) / 180;

// ---- 真机配色 (对照现场照片) ----
const COL_BODY = 0xf1f2f4;   // 白色机身
const COL_PANEL = 0xdde0e4;  // 浅灰盖板
const COL_JOINT = 0x34383f;  // 深灰关节环/端盖
const COL_DARK = 0x23262b;   // 底板/深色件
const COL_CABLE = 0x1a1b1e;  // 黑色线缆
const COL_ACCENT = 0xc23b2e; // 红色标识条

const bodyMat = () => new THREE.MeshStandardMaterial({ color: COL_BODY, metalness: 0.15, roughness: 0.5 });
const panelMat = () => new THREE.MeshStandardMaterial({ color: COL_PANEL, metalness: 0.2, roughness: 0.55 });
const jointMat = () => new THREE.MeshStandardMaterial({ color: COL_JOINT, metalness: 0.55, roughness: 0.4 });
const darkMat = () => new THREE.MeshStandardMaterial({ color: COL_DARK, metalness: 0.5, roughness: 0.55 });
const cableMat = () => new THREE.MeshStandardMaterial({ color: COL_CABLE, metalness: 0.1, roughness: 0.85 });
const accentMat = () => new THREE.MeshStandardMaterial({ color: COL_ACCENT, metalness: 0.3, roughness: 0.45 });

// 沿 +X 的盒 (从 x0 延伸到 x0+len; 截面 w=局部Y, h=局部Z)
function boxX(len, w, h, mat, x0 = 0, z0 = 0, y0 = 0) {
  const m = new THREE.Mesh(new THREE.BoxGeometry(len, w, h), mat);
  m.position.set(x0 + len / 2, y0, z0);
  return m;
}
// 沿 +Z 的圆柱/圆台 (rTop 在 +Z 端)
function cylZ(rTop, rBot, h, mat, z0 = 0, x0 = 0, y0 = 0) {
  const m = new THREE.Mesh(new THREE.CylinderGeometry(rTop, rBot, h, 32), mat);
  m.rotation.x = Math.PI / 2;
  m.position.set(x0, y0, z0 + h / 2);
  return m;
}
// 关节鼓 (旋转轴沿局部 Z, 以 z0 为中心)
function drum(r, thick, mat, z0 = 0, x0 = 0, y0 = 0) {
  const m = new THREE.Mesh(new THREE.CylinderGeometry(r, r, thick, 36), mat);
  m.rotation.x = Math.PI / 2;
  m.position.set(x0, y0, z0);
  return m;
}
// 沿 +X 的圆柱(线缆等)
function tubeX(r, len, mat, x0 = 0, y0 = 0, z0 = 0) {
  const m = new THREE.Mesh(new THREE.CylinderGeometry(r, r, len, 12), mat);
  m.rotation.z = Math.PI / 2;
  m.position.set(x0 + len / 2, y0, z0);
  return m;
}
// 沿 +Z 的细管(线缆)
function tubeZ(r, len, mat, z0 = 0, x0 = 0, y0 = 0) {
  const m = new THREE.Mesh(new THREE.CylinderGeometry(r, r, len, 12), mat);
  m.rotation.x = Math.PI / 2;
  m.position.set(x0, y0, z0 + len / 2);
  return m;
}

export function buildRobot(dh, mounting = "floor") {
  const root = new THREE.Group();
  root.name = "robot-root";
  root.rotation.x = -Math.PI / 2; // DH +Z -> Three +Y (up)
  if (mounting === "ceiling") root.rotation.z = Math.PI;
  else if (mounting === "wall") root.rotation.z = Math.PI / 2;

  const J = dh.joints;
  const d1 = Math.abs(J[0]?.d || 376) / SCALE;   // 基座到 J2 轴高
  const a2 = Math.abs(J[1]?.a || 330) / SCALE;   // 大臂
  const a3 = Math.abs(J[2]?.a || 40) / SCALE;    // 肘偏距
  // ★ P1-E13：a1（肩偏距）/ d4（小臂）/ d6（腕长）这三个 DH 尺寸本文件从没用到 ——
  //   造型一律在下面的关节循环里直接读 j.a / j.d。留着会让人以为"它们参与了建模"。
  //   需要这几个值时读 DH 表本身：J[0].a / J[3].d / J[5].d（单位 mm，除以 SCALE）。

  // ===================== 固定底座 (不随 J1 旋转) =====================
  const Rbase = 0.175; // 官方安装半径 R175
  const plate = cylZ(Rbase, Rbase, 0.022, darkMat(), 0);
  root.add(plate);
  for (let k = 0; k < 6; k++) {
    const ang = (k / 6) * Math.PI * 2;
    const bolt = drum(0.011, 0.028, jointMat(), 0.011, Math.cos(ang) * 0.15, Math.sin(ang) * 0.15);
    root.add(bolt);
  }
  // 铸造立柱(白色, 微锥度)
  const colH = Math.max(d1 - 0.05, 0.08);
  const column = cylZ(0.128, 0.156, colH, bodyMat(), 0.022);
  root.add(column);
  // 红色标识条
  const band = cylZ(0.156, 0.159, 0.028, accentMat(), 0.05);
  root.add(band);
  // 侧面的动力/信号线缆接口盒 (深灰)
  const jbox = new THREE.Mesh(new THREE.BoxGeometry(0.06, 0.1, 0.11), jointMat());
  jbox.position.set(-0.145, 0, d1 * 0.42);
  root.add(jbox);
  // 底部出线弯管
  root.add(tubeZ(0.02, 0.09, cableMat(), 0.02, -0.163, 0.09));

  // ===================== 运动链 (场景图 = 正运动学) =====================
  const joints = [];
  let parent = root;
  let tcpDot = null;
  // ★ J6 法兰坐标系：末端工具（吸盘/夹爪）的挂载点。位置由 DH 的 d6 算出，
  //   与官方模型那条链上的 tcpNode 语义一致 —— 供**残影**挂同一份末端工具用，
  //   避免在外部硬编码 z 偏移（硬编码会把工具埋进腕部筒身里，看不见）。
  let flangeNode = null;
  // ★ 末端工具挂载点：挂在 J6 的**链末端帧 rx**（z=0），与 manager.js
  //   buildArticulatedOfficial 的实体机 tcpNode 完全同位 —— 残影工具与实体机
  //   工具才能在空间上严格重合（2026-10-09 踩坑：挂 flangeNode 再"补回 z"，
  //   因 flangeNode 在关节帧 jg 下、与末端帧差 d6/a6/alpha6 三个变换，
  //   工具被旋进腕部筒身里整块消失，且与示教点位对不上）。
  let eeNode = null;

  for (let i = 0; i < J.length; i++) {
    const j = J[i];
    const jg = new THREE.Group();
    jg.name = "J" + (i + 1);
    parent.add(jg);
    const dz = new THREE.Group(); dz.position.z = j.d / SCALE; jg.add(dz);
    const ax = new THREE.Group(); ax.position.x = j.a / SCALE; dz.add(ax);
    const rx = new THREE.Group(); rx.rotation.x = deg2rad(j.alpha); ax.add(rx);

    const d = Math.abs(j.d) / SCALE;
    // ★ P1-E13：此处原有的 `const a = Math.abs(j.a) / SCALE` 从未被使用（j.a 已在
    //   上面的 ax 组里用过），删掉以免被当成"下面的造型用了它"。

    // -------- J1: 旋转转塔 (位于立柱顶端) --------
    if (i === 0) {
      jg.add(drum(0.13, 0.055, bodyMat(), d1 - 0.017));       // 转盘
      jg.add(drum(0.132, 0.012, jointMat(), d1 - 0.045));     // 轴承环
      joints.push(jg); parent = rx; continue;
    }

    // -------- J2: 大圆柱肩壳 + 大臂 --------
    if (i === 1) {
      // 肩壳: 大圆柱, 轴线 = 局部 Z (水平), 真机上最醒目的大圆筒
      jg.add(drum(0.116, 0.17, bodyMat(), 0));
      jg.add(drum(0.118, 0.016, jointMat(), 0.087));   // 两端深灰端环
      jg.add(drum(0.118, 0.016, jointMat(), -0.087));
      // 大臂: 从肩壳沿局部 +X 延伸到肘部
      jg.add(boxX(a2 + 0.05, 0.115, 0.125, bodyMat(), -0.025, 0.005));
      // 大臂两侧盖板(浅灰)
      jg.add(boxX(a2 - 0.02, 0.085, 0.132, panelMat(), 0.01, 0.005));
      // 顶部黑色线缆(沿大臂)
      jg.add(tubeX(0.013, a2 - 0.04, cableMat(), 0.02, 0.035, 0.07));
      jg.add(tubeX(0.01, a2 - 0.08, cableMat(), 0.04, -0.03, 0.068));
      joints.push(jg); parent = rx; continue;
    }

    // -------- J3: 肘壳 + 肘偏距段 --------
    if (i === 2) {
      jg.add(drum(0.096, 0.135, bodyMat(), 0));
      jg.add(drum(0.098, 0.014, jointMat(), 0.07));    // 端环
      // 肘偏距段: 沿局部 +X (真机零位时向上), 连接到小臂
      jg.add(boxX(a3 + 0.04, 0.095, 0.1, bodyMat(), -0.02, 0));
      joints.push(jg); parent = rx; continue;
    }

    // -------- J4: 小臂 (锥形筒 + 双侧线缆) --------
    if (i === 3) {
      jg.add(drum(0.075, 0.1, bodyMat(), 0));          // 肘端滚转壳
      jg.add(cylZ(0.05, 0.062, d, bodyMat(), 0.01));   // 小臂主体(向腕部收细)
      jg.add(tubeZ(0.012, d - 0.06, cableMat(), 0.03, 0.048, 0.052));  // 前侧线缆
      jg.add(tubeZ(0.012, d - 0.1, cableMat(), 0.05, -0.048, 0.05));   // 后侧线缆
      joints.push(jg); parent = rx; continue;
    }

    // -------- J5: 腕壳 (俯仰轴) --------
    if (i === 4) {
      jg.add(drum(0.06, 0.088, bodyMat(), 0));
      jg.add(drum(0.062, 0.012, jointMat(), 0.046));
      jg.add(drum(0.062, 0.012, jointMat(), -0.046));
      joints.push(jg); parent = rx; continue;
    }

    // -------- J6: 铸造筒 + 法兰 + TCP (滚转轴) --------
    if (i === 5) {
      jg.add(drum(0.052, 0.02, jointMat(), 0.012));    // J6 轴承环
      jg.add(cylZ(0.044, 0.05, Math.max(d, 0.02), bodyMat(), 0.02)); // 腕部筒身
      // 末端法兰 (随 J6 旋转)
      jg.add(cylZ(0.05, 0.05, 0.02, jointMat(), 0.02 + Math.max(d, 0.02)));
      for (let k = 0; k < 6; k++) {
        const ang = (k / 6) * Math.PI * 2;
        jg.add(drum(0.006, 0.024, darkMat(),
          0.03 + Math.max(d, 0.02) + 0.011,
          Math.cos(ang) * 0.035, Math.sin(ang) * 0.035));
      }
      // TCP 标记(绿色)
      tcpDot = new THREE.Mesh(
        new THREE.SphereGeometry(0.012, 16, 12),
        new THREE.MeshStandardMaterial({ color: 0x33dd88, emissive: 0x114422, roughness: 0.4 })
      );
      tcpDot.position.z = 0.045 + Math.max(d, 0.02);
      jg.add(tcpDot);
      // 法兰外表面（法兰圆柱长 0.02、中心在 0.02+max(d,0.02) → 前表面再 +0.01）
      flangeNode = new THREE.Group();
      flangeNode.name = "Flange";
      flangeNode.position.z = 0.02 + Math.max(d, 0.02) + 0.01;
      jg.add(flangeNode);
      // ★ 末端工具挂载点：链末端帧（rx）原点 —— 与实体机 tcpNode 同位（z=0）
      eeNode = new THREE.Group();
      eeNode.name = "EEMount";
      rx.add(eeNode);
      joints.push(jg); parent = rx; continue;
    }

    joints.push(jg);
    parent = rx;

    joints.push(jg);
    parent = rx;
  }

  function applyJoints(qDeg) {
    for (let i = 0; i < joints.length; i++) {
      const off = J[i]?.theta_offset || 0;
      joints[i].rotation.z = deg2rad((qDeg[i] || 0) + off);
    }
  }

  function getTcp() {
    root.updateMatrixWorld(true);
    const v = new THREE.Vector3();
    tcpDot.getWorldPosition(v);
    return { x: v.x * SCALE, y: v.y * SCALE, z: v.z * SCALE };
  }

  // ★ 建好即摆零位：theta_offset（如 J2 的 +90°）必须在这里先应用一次。
  //   否则在第一次 applyRobotPose() 之前，模型是以 rotation.z=0 渲染的"折臂下垂"姿态
  //   —— 画面会闪一下怪姿势，而且此时 J1 子树最低点会掉到基准面下方 90mm，
  //   地面碰撞检测会误判成"穿地(碰撞红闪)"。
  applyJoints(new Array(joints.length).fill(0));

  return { root, applyJoints, getTcp, joints, flangeNode, eeNode };
}
