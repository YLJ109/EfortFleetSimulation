// =====================================================================
// 坐标系换算（**全项目唯一一处**）：机器人基座系(mm, Z 向上) → three.js 场景(Y-up, 米)
//
// 为什么必须单独成文件、并且只留一个函数：
//   机器人模型（程序化 `robotModel.buildRobot` 与官方 GLB）的根节点都是
//     `rotation.x = -Math.PI/2` + `scale = 0.001`，位置再抬到台面 `baseLiftY()`。
//   后端 FK（kinematics.forward_kinematics）给的是**基座系毫米、Z 向上**。
//   两边的换算一旦散落在各处手写，就会出现"某个模块忘了转 90°、或忘了除以 1000"
//   → 方块整体偏移/悬空/埋进台面，而且**看画面几乎看不出来是哪一步错了**。
//   所以这里集中一处，并由 `tools/verify_scene_coords.mjs` 用 three 的 DH 链对拍钉死。
//
// 推导（Rx(−90°) 作用于基座系点 (x, y, z)）：
//   y' = y·cos(−90) − z·sin(−90) = z
//   z' = y·sin(−90) + z·cos(−90) = −y
//   ⇒ (x, y, z) → (x, z, −y)，再除以 1000 变米，最后整体抬 baseLiftY。
// =====================================================================

/**
 * 机器人基座系(mm, Z 向上) → 场景坐标(米, Y 向上)。
 * @param {number} x 基座系 X (mm，指向机器人正前方)
 * @param {number} y 基座系 Y (mm，机器人左手方向)
 * @param {number} z 基座系 Z (mm，向上)
 * @param {number} liftY 场景里机器人的立足面高度(米)，见 manager.baseLiftY()
 * @returns {{x:number,y:number,z:number}} 场景坐标(米)
 */
export function sceneFromBaseMm(x, y, z, liftY = 0) {
  return { x: x / 1000, y: z / 1000 + liftY, z: -y / 1000 };
}

/** 毫米 → 米（单纯单位换算，用于尺寸而不是位置）。 */
export function mmToM(v) {
  return Number(v) / 1000;
}
