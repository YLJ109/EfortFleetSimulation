// =====================================================================
// 用**真实后端数据**（logs/_blocks.json）跑一遍分拣 3D，打印实际摆出来的
// 尺寸与相对位置 —— 用来回答"托盘多大、料多大、相机在哪、会不会压住"。
//
// 为什么要这个：headless 回归用的是**造的**夹具；现场尺寸只能拿真数据算，
// 光看代码里的常量看不出"A 盒和 B 盒会不会撞在一起"。
//
// 数据来源（拿不到就用夹具）：见 docs 里 /api/sorting/blocks 的用法。
// 运行： cd frontend && node tools/preview_sorting3d.mjs [json路径]
// =====================================================================
import { readFileSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { createSortingBlocks } from "../src/three/sortingBlocks.js";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const file = process.argv[2] || join(root, "..", "logs", "_blocks.json");
if (!existsSync(file)) {
  console.error("找不到摆位数据：" + file + "\n（先抓一次 /api/sorting/blocks 存成这个文件）");
  process.exit(2);
}
const d = JSON.parse(readFileSync(file, "utf8"));
const LIFT = 0.18;
const sb = createSortingBlocks(LIFT);
sb.setSlots(d);
const info = sb.info();

const mm = (v) => (v * 1000).toFixed(1);
console.log("== 输入 ==");
console.log("  料尺寸 block_mm      :", d.block_mm.join("×") + " mm");
console.log("  ★ 摆放面 plane_h_mm  :", (d.plane_h_mm ?? "(缺字段→按 0)") + " mm（0 = 站台台面 = 机器人底座平面）");
console.log("  相机底座高 cam_h     :", d.camera_height_mm + " mm");
console.log("  示教法兰 plane_z     :", d.plane_z + " mm（体检用，**不参与摆放高度**）");
console.log("  示教共面偏差         :", d.plane_spread + " mm");
console.log("  重叠风险 pairs       :", (d.overlaps || []).length);
console.log("");
console.log("== 摆出来的东西 ==");
console.log("  场景抬升 liftY(站台面) :", LIFT.toFixed(3) + " m —— 料/托盘/相机都坐在这个面上");
console.log("  A 方块 " + info.a.length + " 块 / B 空位 " + info.b.length + " 个 / 摄像头 " + (info.camera ? "有" : "无"));
info.trays.forEach((t, i) => {
  console.log(`  托盘盒${i === 0 ? "A" : "B"}：宽 ${mm(t[0] / 1000)} × 深 ${mm(t[1] / 1000)} mm，壁高 ${t[2]} mm`);
});
console.log("  相机底座（基座系 mm）:", info.camBase ? info.camBase.join(", ") : "—");
console.log("  提示:", info.warned.length ? info.warned.join("；") : "无");

// ---- 落地检查：托盘底面坐台面、料底面坐托盘内底、相机底面坐台面 —— 一个都不许悬空 ----
console.log("");
console.log("== 落地检查（不悬浮） ==");
{
  const baseY = LIFT + (d.plane_h_mm || 0) / 1000;      // 摆放面（站台台面）
  const pieceY = baseY + (info.trayFloorH || 0) / 1000; // 料面（托盘内底面）
  let worst = 0, n = 0;
  const chk = (bottom, want) => { worst = Math.max(worst, Math.abs(bottom - want)); n++; };
  sb.root.children.forEach((ch) => {
    const u = ch.userData || {};
    if (u.sortName || u.sortSlot) {                     // 料 / B 空位：中心 − 半厚 = 底面
      chk(ch.position.y - (d.block_mm[2] / 1000) / 2, pieceY);
    }
    if (u.sortTray) {                                   // 托盘底板底面
      const f = ch.children[0];
      chk(f.position.y - f.geometry.parameters.height / 2, baseY);
    }
  });
  const cam = sb.root.children.find((c) => c.name === "sorting-camera");
  if (cam) chk(cam.position.y + cam.children[0].position.y
    - cam.children[0].geometry.parameters.height / 2, baseY);
  console.log(`  检查 ${n} 个物体的底面：最大偏差 ${(worst * 1000).toFixed(3)} mm `
    + `${worst < 1e-6 ? "✓ 全部落到实处（不悬浮）" : "✗ 有物体没落到实处"}`);
  console.log(`  （料面比台面高 ${info.trayFloorH || 0}mm = 托盘底板厚；托盘/相机直接坐台面）`);
}

// ---- 相对位置检查：两盒是否分开、盒子是否落在台面范围内 ----
console.log("");
console.log("== 相对位置（基座系 mm，只看 XY） ==");
const trays = sb.root.children.filter((c) => c.userData && c.userData.sortTray);
const ext = trays.map((g) => {
  const [w, dep] = g.userData.sizeMm;
  const [cx, cy] = g.userData.centerMm;      // ★ 盒中心只能从 userData 读（Group 在原点）
  return { cx, cy, w, dep,
           x0: cx - w / 2, x1: cx + w / 2, y0: cy - dep / 2, y1: cy + dep / 2 };
});
ext.forEach((e, i) => console.log(`  盒${i === 0 ? "A" : "B"} 基座 X ${e.x0.toFixed(0)}~${e.x1.toFixed(0)}，Y ${e.y0.toFixed(0)}~${e.y1.toFixed(0)}`));
if (ext.length === 2) {
  const [a, b] = ext;
  const dc = Math.hypot(a.cx - b.cx, a.cy - b.cy);
  // 两盒最近距离 = 中心距 − 各自"朝对方那半"的半宽（矩形分离轴，够用不漏）
  const need = (Math.abs(a.cx - b.cx) ? (a.w + b.w) / 2 : 0)
             + (Math.abs(a.cy - b.cy) ? (a.dep + b.dep) / 2 : 0);
  const gap = dc - Math.sqrt(need * need);
  console.log(`  两盒中心距 ${dc.toFixed(1)} mm，分离所需 ${Math.sqrt(need * need).toFixed(1)} mm`
    + ` → 间隙 ${gap.toFixed(1)} mm ${gap > 0 ? "✓ 不重叠" : "✗ 撞在一起了"}`);
}
sb.dispose();
process.exit(info.warned.length ? 1 : 0);
