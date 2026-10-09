// =====================================================================
// J6 末端执行器（真实工具：法兰 + 两块矩形立板 + 三个白色小吸盘三角排列）
//
// ★★ 单一来源：**官方数模**（manager.js 的 tcpNode）与**残影 Ghost**
//    （ghost.js 的 J6 法兰节点）都调用本函数 —— 从此两边外观不可能漂移。
//
// ★ 尺度约定（务必注意，写错就是 1000 倍事故）：
//   本函数的几何尺寸是"近似 mm 的设计值"，内部再乘 EE_SCALE 放大；
//   它是给**官方那条 0.001 缩放链**用的（官方 root.scale = 0.001）。
//   残影的程序化模型本身是"米"，所以残影侧挂载时必须再乘 0.001
//   （见 ghost.js 的调用点），否则会放大 1000 倍。
//
// 依赖方向：endEffector.js → materials.js（单向，无环）。
//
// ★★ 2026-10-09 用户反馈三改：
//    ① 整体缩小  → EE_SCALE 1.5 → **1.2**
//    ② 红色吸盘 → 三个白色小吸盘，三角形排列（前1后2）
//    ③ 吸盘前移 → 整体前移 + 三角形中心更靠前
// =====================================================================
import * as THREE from "three";
import { skinMat, SKIN_HARD, SKIN_METAL } from "./materials.js";

const EE_BACK = -2;          // 工具最后端 z（设计单位；负 = 压进法兰一点，消除缝隙）
const FLANGE_T = 10;         // 法兰定位盘厚度
const EE_SCALE = 1.2;        // 设计单位 → 真实 mm 的比例（缩小版）
const CUP_FWD = 18;          // ★ 吸盘整体前移量
// ★★ 夹具几何常量提到模块级：吸盘口坐标（suckerLocalMm）与建模必须**同源**
const PLATE_W = 8;           // 立板厚度(x)
const PLATE_H = 36;          // 立板高度(y)
const PLATE_D = 70;          // 立板深度(z，沿法兰轴向)
const PLATE_GAP = 22;        // 两块立板内间距(x 方向净空)
// 三角形吸盘布局参数
const TRI_CUP_R = 5;         // 单个吸盘橡胶圈半径
const TRI_CUP_H = 4;         // 单个吸盘高度
const TRI_SPREAD_X = 14;     // 后方两杯的水平间距（半宽）
const TRI_SPREAD_Z = 10;     // 前杯与后杯的z方向间距
const CUP_MOUNT_Z = 12;      // 吸盘安装板相对立板中心的前移量

// ★ 关键几何参数外露
export const EE_META = {
  back: EE_BACK,
  scale: EE_SCALE,
  cupFwd: CUP_FWD,
  flangeT: FLANGE_T,
};

// ★★ 吸盘口（工件被吸住的位置）在**法兰坐标系**里的局部坐标（真实 mm）。
//   四个吸盘：左右立板外侧各2个（上下排列），开口朝外（±x）
//   返回 +x 侧（右侧）吸盘口位置，工件中心 = 吸盘口 + 半个料厚
export function suckerLocalMm(pieceThickMm = 0) {
  const outer = PLATE_W + PLATE_GAP / 2;           // 立板外表面 x（设计单位）
  const cupX = outer + TRI_CUP_H + 3;              // 吸盘口平面（+x 侧）
  const plateZ = EE_BACK + PLATE_D / 2;
  const cupZ = plateZ + CUP_FWD;                   // 吸盘z位置
  const halfThickDesign = (Number(pieceThickMm) || 0) / 2 / EE_SCALE;
  return [
    (cupX + halfThickDesign) * EE_SCALE,           // 真实 mm，法兰系
    0,                                              // y=0（中间高度）
    cupZ * EE_SCALE,                                // 真实 mm，法兰系
  ];
}

/**
 * 构建 J6 末端工具。
 * @returns {{group: THREE.Group}} group 的局部原点 = J6 法兰面
 */
export function buildEndEffector() {
  const outer = new THREE.Group();
  outer.name = "end-effector";
  outer.rotation.set(0, 0, 0);
  const g = new THREE.Group();
  g.scale.setScalar(EE_SCALE);
  outer.add(g);

  const m = (c, mr, mm) => skinMat(c, mr, mm);

  // ---- 法兰定位盘 + 6 颗螺栓 ----
  const plate = new THREE.Mesh(new THREE.CylinderGeometry(30, 30, FLANGE_T, 36),
                               m(SKIN_METAL, 0.8, 0.28));
  plate.rotation.x = Math.PI / 2;
  plate.position.z = EE_BACK + FLANGE_T / 2;
  g.add(plate);
  for (let k = 0; k < 6; k++) {
    const a = (k / 6) * Math.PI * 2;
    const b = new THREE.Mesh(new THREE.CylinderGeometry(3.5, 3.5, 6, 12), m(SKIN_HARD, 0.85, 0.3));
    b.rotation.x = Math.PI / 2;
    b.position.set(Math.cos(a) * 23, Math.sin(a) * 23, EE_BACK + FLANGE_T + 3);
    g.add(b);
  }

  // ---- 两块矩形立板 ----
  const plateW = PLATE_W;
  const plateH = PLATE_H;
  const plateD = PLATE_D;
  const plateGap = PLATE_GAP;
  const plateZ = EE_BACK + plateD / 2;

  for (let s = -1; s <= 1; s += 2) {
    const p = new THREE.Mesh(
      new THREE.BoxGeometry(plateW, plateH, plateD),
      m(0x9aa2ab, 0.6, 0.4)
    );
    p.position.set(s * (plateW / 2 + plateGap / 2), 0, plateZ);
    g.add(p);
  }

  // ---- 六个白色吸盘：左右立板外侧各3个（三角形：后2前1），开口朝外 ----
  const cupY_top = plateH * 0.2;   // 后两杯上y位置
  const cupY_bot = -plateH * 0.2;  // 后两杯下y位置
  const cupZ_back = plateZ;        // 后两杯z位置（立板中心）
  const cupZ_front = plateZ + CUP_FWD;  // 前一杯z位置（前移）

  function sideCup(side, y, z) {
    // side = -1(左板, 开口朝 -x) 或 +1(右板, 开口朝 +x)
    const grp = new THREE.Group();
    const xOuter = side * (plateW + plateGap / 2);  // 立板外表面 x 坐标

    // 阀体（小圆柱）
    const valve = new THREE.Mesh(
      new THREE.CylinderGeometry(TRI_CUP_R * 0.7, TRI_CUP_R * 0.7, 4, 12),
      m(0xd0d4d8, 0.5, 0.5)
    );
    valve.rotation.z = Math.PI / 2;   // 圆柱轴沿 x
    valve.position.set(xOuter + side * 3, y, z);
    grp.add(valve);

    // 白色波纹吸盘（圆锥，开口朝 ±x）
    const bell = new THREE.Mesh(
      new THREE.ConeGeometry(TRI_CUP_R, TRI_CUP_H, 16),
      m(0xf0f0f0, 0.1, 0.3)
    );
    bell.rotation.z = side * Math.PI / 2;  // 开口朝 ±x
    bell.position.set(xOuter + side * (TRI_CUP_H / 2 + 3), y, z);
    grp.add(bell);

    // 橡胶圈（白色环）
    const rim = new THREE.Mesh(
      new THREE.CylinderGeometry(TRI_CUP_R + 1.5, TRI_CUP_R + 1.5, 2, 16),
      m(0xe8e8e8, 0.2, 0.4)
    );
    rim.rotation.z = Math.PI / 2;
    rim.position.set(xOuter + side * (TRI_CUP_H + 3), y, z);
    grp.add(rim);

    return grp;
  }

  // 左侧3个（后2前1）+ 右侧3个（后2前1）
  g.add(sideCup(-1, cupY_top, cupZ_back));    // 左后上
  g.add(sideCup(-1, cupY_bot, cupZ_back));    // 左后下
  g.add(sideCup(-1, 0, cupZ_front));          // 左前中
  g.add(sideCup(1, cupY_top, cupZ_back));     // 右后上
  g.add(sideCup(1, cupY_bot, cupZ_back));     // 右后下
  g.add(sideCup(1, 0, cupZ_front));           // 右前中

  return { group: outer };
}
