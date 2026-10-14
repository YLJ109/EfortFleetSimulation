// B2 · GLB 瘦身第一步：**只诊断，不动产物**（方案 §4 B2「最小切片」）。
// 不引外部依赖（现场机器上 `npx @gltf-transform` 要走网络，未必拉得动），
// 直接按 glTF 二进制容器规范自己读 JSON chunk。
//
// 为什么要它：本项目 3D 依赖 **J1…J6 命名节点**（three/safety.js 诊断遍历、
// manager.js 关节查找）。任何压缩都必须先确认命名与几何对齐口径不变，
// 所以**先看数**：节点数 / 网格数 / 材质 / 贴图体积 / 有没有动画。
//
// 用法： node tools/inspect_glb.mjs [路径]      （默认 public/models/robot_full.glb）
import fs from "node:fs";
import path from "node:path";

const FILE = process.argv[2] || path.join("public", "models", "robot_full.glb");
const kb = (n) => `${(n / 1024).toFixed(1)} KB`;

function readGlb(buf) {
  if (buf.length < 12) throw new Error("文件太小，不是 GLB");
  const magic = buf.readUInt32LE(0);
  if (magic !== 0x46546c67) throw new Error(`magic 不对：0x${magic.toString(16)}（不是 glTF 二进制）`);
  const version = buf.readUInt32LE(4);
  const total = buf.readUInt32LE(8);
  let off = 12;
  let json = null;
  let bin = 0;
  while (off + 8 <= buf.length) {
    const len = buf.readUInt32LE(off);
    const type = buf.readUInt32LE(off + 4);
    const body = buf.subarray(off + 8, off + 8 + len);
    if (type === 0x4e4f534a) json = JSON.parse(body.toString("utf8"));          // "JSON"
    else if (type === 0x004e4942) bin = len;                                      // "BIN\0"
    off += 8 + len;
  }
  if (!json) throw new Error("没有 JSON chunk");
  return { version, total, json, bin };
}

const buf = fs.readFileSync(FILE);
const { version, total, json, bin } = readGlb(buf);
const L = (k) => (Array.isArray(json[k]) ? json[k] : []);
const nodes = L("nodes"), meshes = L("meshes"), mats = L("materials");
const images = L("images"), texs = L("textures"), anims = L("animations");
const bufferViews = L("bufferViews"), accessors = L("accessors"), buffers = L("buffers");

// 三角面数：每个 primitive 若带 indices 就用 indices 数量 / 3，否则用 POSITION 顶点数 / 3
let tri = 0, prim = 0, withIndices = 0;
for (const m of meshes) {
  for (const p of (m.primitives || [])) {
    prim += 1;
    if (typeof p.indices === "number") { withIndices += 1; tri += Math.floor((accessors[p.indices]?.count || 0) / 3); }
    else tri += Math.floor((accessors[p.attributes?.POSITION]?.count || 0) / 3);
  }
}

// 贴图字节数 = 每张 image 所在 bufferView 的长度（同一张被多处引用只计一次）
const imageBytes = new Map();
for (const t of texs) {
  const img = images[t.source];
  if (!img || typeof img.bufferView !== "number") continue;
  const len = bufferViews[img.bufferView]?.byteLength || 0;
  imageBytes.set(t.source, len);
}
let texTotal = 0;
for (const [idx, len] of imageBytes) { texTotal += len; const i = images[idx]; if (i) i.__bytes = len; }

const named = nodes.filter((n) => typeof n.name === "string" && n.name.trim());
const jointish = named.map((n) => n.name.trim()).filter((s) => /^J[1-6]$/i.test(s));
const dims = {};
for (const a of accessors) {
  if (Array.isArray(a.min) && Array.isArray(a.max) && a.type === "VEC3") {
    for (let k = 0; k < 3; k++) {
      const key = ["x", "y", "z"][k];
      dims[key] = { lo: Math.min(dims[key]?.lo ?? Infinity, a.min[k]), hi: Math.max(dims[key]?.hi ?? -Infinity, a.max[k]) };
    }
  }
}
// 注意：accessor min/max 是**模型局部坐标**，不含节点变换 —— 只能看量级，别当尺寸标定用。

console.log(`文件：${FILE}`);
console.log(`体积：${kb(total)}（实际读取 ${kb(buf.length)}）· GLB v${version} · BIN chunk ${kb(bin)} · JSON chunk ${kb(total - bin)}`);
console.log(`\n节点 ${nodes.length}（有名字的 ${named.length}）· 网格 ${meshes.length} · primitive ${prim}`
  + `（带索引 ${withIndices}）· 三角面约 ${tri.toLocaleString()}`);
console.log(`材质 ${mats.length} · 纹理 ${texs.length} · 图像 ${images.length}`
  + ` · 贴图合计 ${kb(texTotal)}（占 BIN 的 ${bin ? (100 * texTotal / bin).toFixed(1) : "0"}%）`);
console.log(`动画 ${anims.length} · buffer ${buffers.length} · bufferView ${bufferViews.length} · accessor ${accessors.length}`);
console.log(`关节命名节点（J1…J6）：${jointish.length ? jointish.join(", ") : "未找到（改名了？先别压缩）"}`);
console.log(`局部坐标范围（含网格顶点 min/max，未叠加节点变换）：`
  + ` x ${dims.x ? dims.x.lo.toFixed(1) : "—"}…${dims.x ? dims.x.hi.toFixed(1) : "—"}`
  + ` y ${dims.y ? dims.y.lo.toFixed(1) : "—"}…${dims.y ? dims.y.hi.toFixed(1) : "—"}`
  + ` z ${dims.z ? dims.z.lo.toFixed(1) : "—"}…${dims.z ? dims.z.hi.toFixed(1) : "—"}`);
if (images.length) {
  console.log("\n贴图清单：");
  for (const [i, img] of images.entries()) {
    console.log(`  #${i} ${img.mimeType || "?"} ${(img.width ?? "?")}×${img.height ?? "?"} ${kb(img.__bytes || 0)}`
      + ` ${(img.width || 0) >= 512 || (img.height || 0) >= 512 ? "★ ≥512²（方案里说这类才考虑 KTX2/basis）" : ""}`);
  }
}
console.log(`\n结论口径（不做的事）：本脚本**只读**，不写任何产物、不改模型；`
  + `压缩（weld/prune/dedup → meshopt → KTX2）必须等这份报告看过、`
  + `且 verify_official_model.mjs / verify_kinematics.mjs 能证明命名与对齐不变之后再动。`);
