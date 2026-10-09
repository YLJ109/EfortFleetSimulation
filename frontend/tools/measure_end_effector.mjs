// =====================================================================
// 量 J6 末端工具的真实尺寸（相对法兰面，mm）—— headless，无需 WebGL。
//
// 为什么要有它：末端工具的三条现场口径（大小 / 吸盘位置 / 有没有缝）都是
// **尺寸问题**，用肉眼看 3D 截图判断容易反复。这里直接把包围盒量出来：
//   · 沿 z 范围必须**跨过 0**（0 = 法兰面）→ 否则就是"工具没连在 J6 上"
//   · 整体尺寸要与机器人同量级（机器人 0.81m，工具 0.13~0.16m 合理）
//   · 吸盘的 z 必须**大于立板中心 z**（"吸盘向前"）
//
// 调参入口都在 src/three/endEffector.js 顶部：EE_SCALE / EE_BACK / CUP_FWD。
// 运行： cd frontend && node tools/measure_end_effector.mjs
// =====================================================================
import * as THREE from "three";
import { buildEndEffector, EE_META } from "../src/three/endEffector.js";

// materials.js 的 skinMat 会建 CanvasTexture → 在 node 里给 document 打最小桩
if (typeof globalThis.document === "undefined") {
  globalThis.document = {
    createElement: () => ({
      width: 0, height: 0,
      getContext: () => ({
        fillRect() {}, beginPath() {}, arc() {}, fill() {}, strokeRect() {},
        createRadialGradient: () => ({ addColorStop() {} }),
        set fillStyle(v) {}, set strokeStyle(v) {}, set lineWidth(v) {},
      }),
    }),
  };
}

const ee = buildEndEffector();
ee.group.scale.setScalar(0.001);        // 与残影同一条 0.001 缩放链（世界单位 = 米）
const box = new THREE.Box3().setFromObject(ee.group);
const mm = (v) => (v * 1000).toFixed(1);

const plateCenter = EE_META.scale * (EE_META.back + 45);            // 立板中心 z(mm)
const cupZ = EE_META.scale * (EE_META.back + 45 + EE_META.cupFwd);  // 吸盘 z(mm)

console.log("== 参数（src/three/endEffector.js） ==");
console.log("  EE_META :", JSON.stringify(EE_META));
console.log("");
console.log("== 实测尺寸 ==");
console.log(`  整体 : ${mm(box.max.x - box.min.x)} 宽 × ${mm(box.max.y - box.min.y)} 高`
  + ` × ${mm(box.max.z - box.min.z)} 长（沿法兰轴向 z）`);
console.log(`  沿 z : ${mm(box.min.z)} … ${mm(box.max.z)} mm（0 = 法兰面）`);
console.log(`  吸盘 : z = ${cupZ.toFixed(1)} mm（立板中心 z = ${plateCenter.toFixed(1)} mm）`);
console.log("");
console.log("== 判据 ==");
const hasGap = box.min.z > 0.0005;
const buried = box.min.z < -0.005;
console.log(`  ${hasGap ? "✗" : "✓"} 后缘跨过法兰面（不悬空）：min.z = ${mm(box.min.z)} mm`);
console.log(`  ${buried ? "✗" : "✓"} 未埋进腕部（≥ −5mm）`);
console.log(`  ${cupZ > plateCenter ? "✓" : "✗"} 吸盘在立板中心之前（向前）`);
process.exit(hasGap || buried || cupZ <= plateCenter ? 1 : 0);
