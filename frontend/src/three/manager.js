// =====================================================================
// Three.js 单例管理器 —— 全局单 WebGLRenderer + **单场景 + 单套模型**。
//
// 设计（对应"三视图统一模板"）：
//   - 只有 1 个 Scene / Camera / OrbitControls，只有 1 份程序化模型 + 1 份官方数模。
//   - 切视图 = 把同一张 canvas 在三个容器之间搬移，场景与模型原地不动。
//   - 官方模型直接用 GLB **原件**构建（不 clone），Object3D.attach() 的源父级
//     天然是 GLB 根节点（rotX(-90°)+scale(0.001)），彻底消灭"第二份模型放大
//     1000 倍"这类 bug —— 份数只有 1，就没有"哪一份写错了"的问题。
// =====================================================================
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { RoomEnvironment } from "three/addons/environments/RoomEnvironment.js";
import { buildRobot } from "../robot/robotModel.js";
import { FALLBACK_DH } from "../config.js";
import { buildCell, PLATFORM_H, PLATFORM_TOP_SIZE } from "./cell.js";
// lab.js 已移除：场景改为开放科技感背景（网格 + 粒子光晕）
import { SafetyFence, STATE_RANK } from "./safety.js";
import { createGhost, GHOST_COLOR_B } from "./ghost.js";
import { buildOwnerMap } from "./jointOwner.js";
import { SKIN_BODY, SKIN_HARD, SKIN_JOINT, SKIN_LOGO, SKIN_METAL, skinMat } from "./materials.js";
import { buildEndEffector, suckerLocalMm } from "./endEffector.js";
import { createSortingBlocks } from "./sortingBlocks.js";

// ---- 单例资源 ----
let renderer = null;
let scene = null;
let camera = null;
let controls = null;
let envTex = null;

const containers = {};   // view -> HTMLElement（canvas 的落点）
let activeView = "live";

let robot = null;        // 程序化模型（兜底） ×1
let official = null;     // 官方数模（可动） ×1
let fence = null;        // 安全围栏
let cell = null;         // 地板 + 网格 + 阴影 + 地标线
let techBg = null;       // 科技感背景（网格 + 粒子）
let ghost = null;        // 残影 A(预演分身)：程序化模型半透明版，负责回答"要去的地方会不会撞"
// ★ 残影 B（对比用）：让"两个点位同时看"成为可能（如 正上位 vs 原料位、A槽 vs B槽）。
//   它与 A **同源同构**（同一个 createGhost，只换颜色），但**不参与围栏判定** ——
//   安全结论永远只反映 A（= 当前正在评估的那个目标），避免两个残影把结论搅混。
let ghostB = null;
let sim = null;          // 模拟摆放物体沙盒
// ★ 分拣 3D 可视化（18 槽位方块 + C 区摄像头）：按需创建（只有分拣页会拉它），
//   与残影/围栏**互不影响** —— 它只是"把 state 画出来"，不参与任何安全判定。
let sorting = null;
let activeDh = null;
let activeMounting = "floor";

const deg2rad = (d) => (d * Math.PI) / 180;

// GBK 名称还原: GLTFLoader 把 GBK 字节按 latin1 读出(零件名显示为乱码),
// 这里转回字节再用 gbk 解码, 以便按"转座/大臂/手腕体/手腕/法兰"精确归类连杆。
function fixGbkName(s) {
  if (!s) return "";
  try {
    const bytes = Uint8Array.from(s, (ch) => ch.charCodeAt(0) & 0xff);
    return new TextDecoder("gbk").decode(bytes);
  } catch (e) {
    return s;
  }
}

// 官方数模零件名 -> 连杆号 (0=固定底座, 1~6=J1~J6 所属连杆)
const NAME_TO_LINK = { "转座": 1, "大臂": 2, "手腕体": 4, "手腕": 5, "法兰": 6 };

// ---------------------------------------------------------------------
// 官方数模配色（对照现场真机照片：白色漆面机身 + 深灰五金 + 铝银法兰）
// ★ 材质与"J6 末端工具"已抽到共享模块（materials.js / endEffector.js），
//   因为**残影 Ghost 也要用同一份**，避免两边外观漂移。
// ---------------------------------------------------------------------

function skinOf(p) {
  if (p.name === "法兰") return skinMat(SKIN_METAL, 0.80, 0.28);
  if (p.maxDim >= 200) return skinMat(SKIN_BODY, 0.15, 0.42);
  if (p.maxDim >= 60) return skinMat(SKIN_JOINT, 0.45, 0.38);
  return skinMat(SKIN_HARD, 0.55, 0.40);
}

let logoTex = null;

/** 生成 EFORT 红色标识贴图（透明底，只用 canvas，不依赖外部素材）。 */
function makeLogoTexture() {
  if (logoTex) return logoTex;
  const c = document.createElement("canvas");
  c.width = 512; c.height = 160;
  const g = c.getContext("2d");
  g.clearRect(0, 0, c.width, c.height);
  g.fillStyle = SKIN_LOGO;
  g.font = "italic 900 116px Arial, Helvetica, sans-serif";
  g.textAlign = "center";
  g.textBaseline = "middle";
  g.fillText("EFORT", c.width / 2, c.height / 2 + 4);
  logoTex = new THREE.CanvasTexture(c);
  logoTex.colorSpace = THREE.SRGBColorSpace;
  logoTex.anisotropy = 4;
  return logoTex;
}

/**
 * 小臂（J4 连杆 / 手腕体）红色 EFORT 标识，贴在 **±Y 两侧面（上/下面）**。
 *
 * 位置**不写死**：由 J4 连杆零件的几何包围盒实测得出（单位 mm / 基座坐标系）：
 *   - 贴在 ±Y 面（上/下两面），平面沿前臂长度方向(X)铺开，中心取
 *     (cx, ±(半高 + 1.5), cz)，cx 为连杆 X 中点(略偏右)、cz 为 Z(深度) 中点。
 *   - 两面各自绕 X 转 ±90° 朝外（上面 -90° / 下面 +90°），文字沿前臂读向一致、不镜像。
 *   - 标识长宽按 bbox 自适应并夹在 [80,200] × [26,48] mm，保证比例协调。
 */
function buildArmLogo(width, height, cx, cz, yTop, yBot) {
  const grp = new THREE.Group();
  grp.name = "arm-logo";
  const mat = new THREE.MeshStandardMaterial({
    map: makeLogoTexture(), transparent: true, roughness: 0.42, metalness: 0.0,
    polygonOffset: true, polygonOffsetFactor: -2, polygonOffsetUnits: -2,
  });
  const geo = new THREE.PlaneGeometry(width, height);
  [-1, 1].forEach((s) => {
    const m = new THREE.Mesh(geo, mat);
    // s>0 → 上面(+Y，绕 X 转 -90° 使法线朝 +Y)；s<0 → 下面(-Y，绕 X 转 +90°)
    m.position.set(cx, s > 0 ? yTop : yBot, cz);
    m.rotation.set(s > 0 ? -Math.PI / 2 : Math.PI / 2, 0, 0);
    grp.add(m);
  });
  return grp;
}

// ---------------------------------------------------------------------
// WebGL 上下文健康状态：lost / ok
// ★ 以前 context lost 只打一条 console.warn，界面继续黑着，现场只能靠 F5 自救。
//   现在把它做成一条可订阅的状态：App.vue 据此显示"正在恢复"浮条，
//   并在恢复后自动重启渲染循环 —— 用户不必再刷新页面。
// ---------------------------------------------------------------------
let glStateCb = null;
let glState = "ok";

export function onGlState(cb) { glStateCb = cb; }

function setGlState(s) {
  if (glState === s) return;
  glState = s;
  if (glStateCb) { try { glStateCb(s); } catch (e) { /* 忽略订阅方异常 */ } }
}

export function getGlState() { return glState; }

function ensureRenderer() {
  if (renderer) return renderer;
  // ★ 不指定 powerPreference:high-performance —— 现场多标签页 + MJPEG 流会把
  //   D3D 显存挤爆（CONTEXT_LOST / 纹理分配失败），默认档反而更稳。
  renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: "default" });
  // 像素比封顶 1.25：4K 屏上 1.5 的纹理体积是 1.25 的 1.44 倍，是显存爆掉的主因之一。
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.25));
  // ★ 画面打磨：ACES 电影级色调映射 + 明确 sRGB 输出，让金属反光与白色机身
  //   更有层次、暗部不死黑（默认 NoToneMapping 偏灰、高光易过曝）。
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.06;
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.domElement.style.display = "block";
  renderer.domElement.style.width = "100%";
  renderer.domElement.style.height = "100%";
  renderer.domElement.addEventListener("webglcontextlost", (e) => {
    // ★ preventDefault 是"允许浏览器尝试恢复上下文"的前提，去掉它就只能刷新页面。
    e.preventDefault();
    setGlState("lost");
    console.warn("WebGL 上下文丢失 —— 等待浏览器自动恢复（无需刷新）");
  });
  renderer.domElement.addEventListener("webglcontextrestored", () => {
    // three.js 内部会重建 GL 状态并重新上传几何/纹理，这里只需把画布尺寸与
    // 像素比重新对齐（丢失期间容器可能已变宽），再通知界面恢复渲染。
    //
    // 审计修复 P0-ui-2：恢复瞬间若容器还是 display:none（正在切视图），
    // resizeView 会因尺寸 0 **早退**，画布尺寸就永远停在丢失前的旧值 ——
    // 补一条重试；尺寸正常则立刻强制渲染一帧，避免"上下文已恢复但这一帧是
    // 清空过的空帧"再黑闪一下。
    if (camera && !resizeView()) scheduleSizeFixup();
    setGlState("ok");
    if (camera) renderFrame();
    console.info("WebGL 上下文已恢复，3D 渲染继续");
  });
  return renderer;
}

/** 科技感背景：大范围地面网格 */
function buildTechBackground(scene) {
  // 大范围地面网格（青色，向外渐隐）
  const gridHelper = new THREE.GridHelper(20, 40, 0x2fd6f0, 0x1a5f6f);
  gridHelper.position.y = -0.001;
  gridHelper.material.opacity = 0.12;
  gridHelper.material.transparent = true;
  scene.add(gridHelper);

  techBg = { grid: gridHelper };
}

/** 懒创建唯一场景（不依赖容器，容器只用于 canvas 落点）。 */
function ensureScene() {
  if (scene) return scene;
  const r = ensureRenderer();
  scene = new THREE.Scene();
  scene.background = new THREE.Color(0x04070d);  // Sentinel 主题深蓝黑背景
  scene.fog = new THREE.Fog(0x04070d, 6, 18);   // 开放场景：远处渐隐入深蓝黑

  camera = new THREE.PerspectiveCamera(50, 1, 0.01, 120);
  camera.position.set(1.7, 1.35, 1.75);

  controls = new OrbitControls(camera, r.domElement);
  controls.target.set(0, 0.45, 0);
  controls.minDistance = 0.6;
  controls.maxDistance = 9;          // 房间 14m，取景可拉远仍不穿墙
  controls.enableDamping = true;
  controls.enabled = true;

  if (!envTex) {
    try {
      const pmrem = new THREE.PMREMGenerator(r);
      envTex = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
      pmrem.dispose();
    } catch (e) {
      console.warn("环境贴图生成失败, 用纯灯光:", e);
    }
  }
  if (envTex) {
    scene.environment = envTex;
    scene.environmentIntensity = 0.65;
  }
  scene.add(new THREE.HemisphereLight(0xffffff, 0x2a323c, 0.85));
  const dir = new THREE.DirectionalLight(0xfff6e8, 0.95);   // 略暖的主光
  dir.position.set(1.5, 2.5, 1.0);
  scene.add(dir);
  const fill = new THREE.DirectionalLight(0xdde8ff, 0.3);   // 略冷的补光（让金属/地坪有层次）
  fill.position.set(-2.0, 1.8, -1.5);
  scene.add(fill);

  cell = buildCell();
  scene.add(cell.group);

  // 科技感背景：大范围地面网格 + 氛围粒子
  buildTechBackground(scene);

  fence = new SafetyFence(scene);
  // 机器人站在站台上 → 报警垫改铺到**台面**上（否则会被站台本体整块盖住）。
  // 落地安装才铺台面；吊装/侧装保持铺地坪。
  if (baseLiftY() > 0) {
    fence.setPadPlane({ y: PLATFORM_H + 0.004, size: PLATFORM_TOP_SIZE });
  }
  // ★ 模拟物体（盒子、分拣物、摄像头）已移除，不再创建
  return scene;
}

// ---- 安全状态广播 ----
let safetyCb = null;
let lastSafetyKey = "";

export function onSafety(cb) { safetyCb = cb; }

/**
 * 热更新围栏配置（不需要重建场景）。
 * @param {object} cfg 配置
 * @param {boolean} applyCam 是否同时把相机移到配置里的视角。
 *   只在启动时/点「复位视角」时传 true —— 否则每拖一次滑块视角就被拽回去。
 */
export function setSafetyConfig(cfg, applyCam = false) {
  ensureScene();
  fence.applyConfig(cfg);
  sizePlatformToFence(cfg);
  if (applyCam && cfg && cfg.camera) {
    applyCameraPose(cfg.camera.position, cfg.camera.target);
  }
}

/**
 * 站台台面与四根角柱对位：取主工作区围栏 x/z 半宽中较小者，把台面四边
 * 正好压到四根角柱中心（默认半宽 0.82 → 台面 1.64×1.64）。缩放围绕台面中心，
 * 高度方向不动，底部脚印围栏也随之扩到同一位。
 */
function sizePlatformToFence(cfg) {
  if (!cell || !fence) return;
  const zones = (cfg && cfg.zones) || [];
  const z = zones.find((zz) => zz && zz.enabled !== false) || zones[0];
  const hx = Number(z && z.half && z.half.x) || 0.82;
  const hz = Number(z && z.half && z.half.z) || 0.82;
  const half = Math.max(0.3, Math.min(2, Math.min(hx, hz)));
  const outerHalf = PLATFORM_TOP_SIZE / 2;
  const f = half / outerHalf;
  const plat = cell.group.getObjectByName("platform");
  if (plat) plat.scale.set(f, 1, f);
  fence.setPadPlane({ y: PLATFORM_H + 0.004, size: half * 2 });
  if (sim) sim.setBase(PLATFORM_H, half);   // 沙盒盒/摄像头/落点吸合跟随站台
}

/** 当前生效的机器人根节点（围栏包围盒与可见性都用它）。 */
export function getActiveRoot() {
  if (official) return official.root;
  return robot ? robot.root : null;
}

/**
 * 地面碰撞检测对象：J1 连杆子树（不含固定底座）。
 * 底座本来就贴在地面上（min.y≈0），整模型取包围盒会永远"碰撞地面"，
 * 所以只取 J1 及以上部分 —— 它的最低点就是真正可能撞地的部位（小臂/腕/夹爪）。
 */
export function getGroundRoot() {
  const m = official || robot;
  return m && m.joints && m.joints[0] ? m.joints[0] : null;
}

/**
 * 机器人立足面高度(m)：落地安装时站在 cell 的**站台台面**上(PLATFORM_H)，
 * 吊装/侧装不抬。地面碰撞检测的"零距离参考面"与模型抬升量必须取同一个值。
 */
function baseLiftY() {
  return (activeMounting === "ceiling" || activeMounting === "wall") ? 0 : PLATFORM_H;
}

/**
 * 安全评估的**唯一入口** —— "当前生效位姿"到底是哪一具身体：
 *
 *   残影可见（正在预演下一个点位）→ 量**残影的目标位姿**（终点）；
 *   否则                        → 量实体机的**当前位姿**（实时监控）。
 *
 * ★ 为什么必须合成一个入口：围栏面板/地面垫的变色、上报后端的安全状态、
 *   以及执行域的下发门控，三者必须**同源**。以前面板量实体机、门控又量别处，
 *   才会出现"面板红了但照样能下发"这种自相矛盾。
 * ★ `source` 一并返回：执行域据此区分"这是预演结论"还是"这是实时读数"——
 *   只有预演结论（source === "ghost"）才构成点位/示教/程序的拦截依据。
 *
 * @param {number} tSec 秒（闪烁相位）
 * @returns {{state, ratio, clearance, zoneId, zoneName, zones, source}|null}
 */
export function evalSafety(tSec = performance.now() / 1000) {
  ensureScene();
  if (!fence) return null;
  const previewing = !!(ghost && ghost.visible);
  // ★ 残影测量走 withTarget：临时摆到**目标姿态**量完立刻还原，视觉上无跳变。
  //   地面检测同样只取 J1 以上子树（与实体机口径一致），否则贴地底座会永远"碰撞"。
  const r = previewing
    ? ghost.withTarget((root, joints) =>
        fence.update(root, joints && joints[0] ? joints[0] : null, tSec, baseLiftY()))
    : fence.update(getActiveRoot(), getGroundRoot(), tSec, baseLiftY());
  return r ? { ...r, source: previewing ? "ghost" : "robot" } : null;
}

function updateSafety(tSec) {
  if (!fence) return;
  const r = evalSafety(tSec);
  if (!r) return;
  // 残影泛红：预演终点危险/碰撞 → 整体变红，一眼看出"这个点位不能去"。
  // 量的是实体机时一律解除（泛红只表达预演结论，不表达实时读数）。
  if (ghost) ghost.setAlarm(r.source === "ghost" && STATE_RANK[r.state] >= STATE_RANK.danger);
  const key = `${r.source}|${r.state}|${r.zoneId}|${r.clearance.toFixed(3)}`;
  if (key !== lastSafetyKey) {
    lastSafetyKey = key;
    if (safetyCb) safetyCb(r);
  }
}

/** 登记一个视图的 3D 容器（各组件 onMounted 调用）。同一个 scene，不新建。 */
export function registerView(view, container) {
  ensureScene();
  containers[view] = container;
  if (activeView === view) switchView(view);
  return container;
}

/** 构建程序化机器人模型（只建一份），dh 已兜底。 */
export function initRobots(dh, mounting = "floor") {
  ensureScene();
  const dhOk = dh && dh.joints && dh.joints.length === 6 ? dh : FALLBACK_DH;
  activeDh = dhOk;
  activeMounting = mounting || "floor";
  if (!robot) {
    robot = buildRobot(dhOk, activeMounting);
    robot.root.position.y = baseLiftY();   // 站到站台台面上
    scene.add(robot.root);
    // ★ 程序化模型是"米"坐标系 → 局部数值 /1000（官方那条是 0.001 缩放链，用 mm）
    attachPieceAnchor(robot.flangeNode, 0.001);
  }
  if (!ghost) {
    // ★ 残影走程序化模型（绝不 clone 官方 GLB 单 mesh，那会放大 1000 倍）
    ghost = createGhost(dhOk, activeMounting, baseLiftY());
    scene.add(ghost.root);
  }
  if (!ghostB) {
    ghostB = createGhost(dhOk, activeMounting, baseLiftY(), GHOST_COLOR_B);
    ghostB.setVisible(false);           // 默认藏着，需要对比时才由界面打开
    scene.add(ghostB.root);
  }
  applyAppearance();
}

// ---------------------------------------------------------------------
// 姿态平滑：真机数据 20Hz、键盘/滑块是跳变，直接给模型会出现"一顿一顿"。
// 这里维护 target → current 的一阶指数收敛，每帧以 60fps 逼近，动作就丝滑了。
// 收敛速率 k=14 /s：约 200ms 到位，肉眼跟手但不抖动。
// ---------------------------------------------------------------------
const SMOOTH_K = 14;
const qTarget = [0, 0, 0, 0, 0, 0];
const qCurrent = [0, 0, 0, 0, 0, 0];
let qReady = false;
let lastFrameMs = 0;

function pushPose(q) {
  if (robot) robot.applyJoints(q);
  if (official) official.applyJoints(q);
}

/**
 * 应用关节角(度) —— 程序化与官方两套同步驱动（只有一份，切外观不丢姿态）。
 * 只更新"目标姿态"，实际模型由渲染循环逐帧平滑逼近。
 */
export function applyRobotPose(qDeg) {
  for (let i = 0; i < 6; i++) qTarget[i] = qDeg && qDeg[i] ? qDeg[i] : 0;
  if (!qReady) {
    for (let i = 0; i < 6; i++) qCurrent[i] = qTarget[i];
    qReady = true;
    pushPose(qCurrent);
  }
}

/** 每帧推进平滑（返回是否有位移）。 */
function stepSmoothing(dt) {
  if (!qReady) return false;
  const k = 1 - Math.exp(-dt * SMOOTH_K);
  let moved = false;
  for (let i = 0; i < 6; i++) {
    const d = qTarget[i] - qCurrent[i];
    if (Math.abs(d) < 1e-4) { qCurrent[i] = qTarget[i]; continue; }
    qCurrent[i] += d * k;
    moved = true;
  }
  if (moved) pushPose(qCurrent);
  return moved;
}

/**
 * 读取当前生效模型的末端 TCP（世界坐标 mm）。
 * ★ 用**目标姿态**计算：平滑动画只是视觉过渡，IK/坐标显示必须拿到真实指令值，
 *   否则连续点动会累积滞后误差。算完立刻把动画姿态还原。
 */
export function getTcp() {
  const r = official || robot;
  if (!r || !r.getTcp) return { x: 0, y: 0, z: 0 };
  pushPose(qTarget);
  const t = r.getTcp();
  pushPose(qCurrent);
  return t;
}

// =====================================================================
// ★★ 工件锚点（piece-anchor）：挂在 J6 末端工具的**吸盘口**上，供分拣 3D
//    把"手上那块料"贴到吸盘上（2026-10-09 用户反馈："吸气的时候 3D 物体贴着
//    机器人的 J6 那个工具的红色吸盘一起走，不要消失"）。
//
//    为什么不能挂在法兰中心：本工具从法兰沿 z 伸出 132mm、两个红色吸盘装在
//    左右立板外侧（开口朝 ±x）—— 法兰中心那个位置**在腕部实体内部**，
//    工件放那儿会被腕筒挡住，看起来就是"一吸气工件就消失"。
//    锚点位置由 endEffector.suckerLocalMm() 给（与建模几何同源，改几何自动跟着走）。
//
//    ★ 单位坑：官方模型挂在 `root.scale = 0.001` 的链里，局部数值用 **mm**；
//      程序化模型本身是**米**，局部数值必须 /1000。两边差别 1000 倍，写错就是灾难。
// =====================================================================
let eeAnchor = null;

function attachPieceAnchor(node, localUnitPerMm) {
  if (!node) return;
  const [x, y, z] = suckerLocalMm(0);          // 吸盘口（不含料厚）
  const a = new THREE.Object3D();
  a.name = "piece-anchor";
  a.position.set(x * localUnitPerMm, y * localUnitPerMm, z * localUnitPerMm);
  node.add(a);
  eeAnchor = a;
}

/**
 * 吸盘口锚点的世界位置(mm, 场景系) + 世界朝向。
 * 每帧由 renderFrame 喂给分拣 3D —— 料因此**贴着吸盘、随 J6 姿态一起走**。
 * @returns {{x:number,y:number,z:number,q:[number,number,number,number]}|null}
 */
export function getPieceAnchor() {
  if (!eeAnchor) return null;
  eeAnchor.updateMatrixWorld(true);
  const p = new THREE.Vector3();
  const q = new THREE.Quaternion();
  const s = new THREE.Vector3();
  eeAnchor.matrixWorld.decompose(p, q, s);
  return { x: p.x * 1000, y: p.y * 1000, z: p.z * 1000, q: [q.x, q.y, q.z, q.w] };
}

/** 切换活动视图：把 canvas 搬到该视图容器 + resize。场景与模型不动。 */
export function switchView(view) {
  ensureScene();
  activeView = view;
  const el = containers[view];
  if (!el) return;
  if (renderer.domElement.parentElement !== el) el.appendChild(renderer.domElement);
  // 审计修复 P0-ui-3：调用方（App.vue setView）是**同步**调用的，此时 Vue 还没把
  // 新视图 v-show 显示出来（目标容器仍 display:none → clientWidth/Height 为 0 →
  // resizeView 早退），画布停在旧缓冲尺寸/空帧上；等 ResizeObserver 发现尺寸变化
  // 还要再走 MonitorLayout 的 240ms debounce 才 setSize —— 这段窗口正是
  // "3D 偶尔黑屏闪一下"的来源之一。
  // 这里搬完画布立刻校正 + 强制渲染一帧；若此刻还没尺寸就用 rAF 连续补，
  // Vue 一翻页即可在**下一次绘制之前**完成 setSize + 重绘，不再留黑帧窗口。
  if (!resizeView()) scheduleSizeFixup();
  else if (glState === "ok") renderFrame();   // 搬移画布后补一帧，避免合成层清空露出黑底
}

// ---- 审计修复 P0-ui-3：切视图后的尺寸补救（容器 0 尺寸时的重试） ----
let sizeFixRaf = 0;
/**
 * 容器暂时没有尺寸（v-show 尚未生效 / 正在布局）时，逐帧重试 resizeView，
 * 一旦有尺寸立刻 setSize + 强制渲染一帧。上限 60 帧（约 1s）后放弃 ——
 * 之后的尺寸修正交回 ResizeObserver（那是"宽度变化"场景的正路）。
 */
function scheduleSizeFixup() {
  if (sizeFixRaf) cancelAnimationFrame(sizeFixRaf);
  let tries = 60;
  const tick = () => {
    sizeFixRaf = 0;
    if (resizeView()) {
      if (glState === "ok") renderFrame();
      return;
    }
    if (tries-- > 0) sizeFixRaf = requestAnimationFrame(tick);
  };
  sizeFixRaf = requestAnimationFrame(tick);
}

// 当前已生效的像素比：只有它变了才调 setPixelRatio。
// ★ setPixelRatio 内部会重建绘制缓冲，每帧调等于每帧抖一次缓冲（黑闪）。
let lastPixelRatio = 0;

/**
 * 按当前活动容器的实际尺寸重设画布。
 * @returns {boolean} 是否真的完成 resize；false = 容器不存在或尺寸为 0（早退）。
 *   审计修复 P0-ui-3：把"早退"变成可判据的返回值，调用方才能补救
 *   （否则 0 尺寸早退后无人知晓，画布一直停在旧尺寸上）。
 */
export function resizeView() {
  ensureScene();
  const el = containers[activeView];
  if (!el) return false;
  const w = el.clientWidth, h = el.clientHeight;
  if (w === 0 || h === 0) return false;
  // 把窗口拖到另一块显示器时 devicePixelRatio 会变，这时才需要重新设置。
  const pr = Math.min(window.devicePixelRatio || 1, 1.25);
  if (pr !== lastPixelRatio) { renderer.setPixelRatio(pr); lastPixelRatio = pr; }
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
  return true;
}

export function renderFrame() {
  ensureScene();
  const now = performance.now();
  const dt = lastFrameMs ? Math.min(0.1, (now - lastFrameMs) / 1000) : 1 / 60;
  lastFrameMs = now;
  stepSmoothing(dt);                 // 姿态平滑逼近 → 动作丝滑
  if (ghost) ghost.step(dt);         // 残影预演：把自己的姿态"飘"到目标点位
  if (ghostB) ghostB.step(dt);        // 对比残影同理（平滑飘过去）
  controls.update();
  // 实验室已移除，无智能剖切
  updateSafety(now / 1000);
  if (sim) {
    const t = getTcp();                        // mm → 米，喂给模拟沙盒
    sim.tick({ x: t.x / 1000, y: t.y / 1000, z: t.z / 1000 });
  }
  // 分拣：摄像头闪烁 + 手上方块**贴着吸盘**跟随末端（用吸盘口锚点，不是法兰中心）
  if (sorting) sorting.step(dt, getTcp(), getPieceAnchor());
  renderer.render(scene, camera);
}

// ---------------------------------------------------------------------
// 残影预演（下一个点位）：把半透明分身摆到目标点位。
// ★ 残影**不参与实体机的实时读数**（getActiveRoot/getGroundRoot 只认实体机），
//   但它是**下发判据**的来源 —— 见 evalSafety()：残影可见时围栏/地面按它的
//   目标位姿评估，面板变红 = 残影泛红 = 这个点位不能去。
// ---------------------------------------------------------------------
/** 预演下一个点位：qDeg 为目标关节角(度)。自动显示残影。 */
export function setGhostPose(qDeg) {
  if (ghost) ghost.setPose(qDeg);
}

/** 显隐残影。 */
export function setGhostPoseB(qDeg) {
  if (ghostB) ghostB.setPose(qDeg);
}
export function showGhostB(v) {
  if (ghostB) ghostB.setVisible(v);
}
export function ghostBVisible() { return !!(ghostB && ghostB.visible); }

export function showGhost(v) {
  if (ghost) ghost.setVisible(v);
}

export function ghostVisible() { return !!(ghost && ghost.visible); }

// ---------------------------------------------------------------------
// 分拣 3D 可视化（方块/空槽/摄像头）—— 由分拣页驱动。
// ★ 位置全部来自后端 FK（示教点位），**不是相机识别的实际位置**；
//   坐标换算唯一入口见 three/sceneCoords.js（有 headless 对拍钉死）。
// ---------------------------------------------------------------------
/** 灌入槽位数据（来自 /api/sorting/blocks）。首次调用时按需建对象。 */
export function setSortingSlots(payload) {
  ensureScene();
  if (!sorting) {
    sorting = createSortingBlocks(baseLiftY());
    scene.add(sorting.root);
  }
  sorting.setSlots(payload);
  return sorting.info();
}

/** 按 /api/sorting/state 更新方块（幂等；同状态不重复改场景）。 */
export function updateSortingState(state) {
  if (!sorting) return;
  sorting.update(state);
}

/**
 * 井字棋棋盘覆盖：B 区按格子显隐方块、A 区按已取用隐藏料（方案 §6.3）。
 * ★ 3D 未装填时空操作；传 null 清除覆盖（回到分拣显示）。
 * @param {{board?:Array, used?:Array, holding?:boolean}|null} o
 */
export function setTttBoardState(o) {
  if (!sorting) return;
  sorting.setTtt(o);
}

/** 分拣可视化的显隐（离开分拣页就藏起来，避免在别的视图里"多出一堆方块"）。 */
export function showSortingBlocks(v) {
  if (sorting) sorting.setVisible(v);
}

/** 分拣可视化是否已建（供页面判断要不要先拉 /blocks）。 */
export function sortingBlocksReady() { return !!sorting; }

/** 分拣可视化自检信息（回归用）。 */
export function sortingBlocksInfo() { return sorting ? sorting.info() : null; }

// ---------------------------------------------------------------------
// 关节高亮：悬停某个关节读数时，把 3D 里对应的关节段打上蓝色自发光。
// ★ 按**材质**记录原始 emissive（GLB 里多个 mesh 常共用材质，按 mesh 记会串色）。
// ---------------------------------------------------------------------
const HL_COLOR = 0x2f9bff;
const hlMats = new Map();   // material -> 原始 emissive 色值

// 模型 -> (mesh -> 驱动它的关节下标)。按模型缓存，模型是静态的只算一次。
// ★ 归属算法单独放在 jointOwner.js：那是"按状态染色"的判定逻辑，
//   最容易静默回归，需要能在 node 里直接跑断言（见 tools/verify_highlight.mjs）。
const HL_OWNER = new WeakMap();

function ownerMapOf(model) {
  let m = HL_OWNER.get(model);
  if (!m) { m = buildOwnerMap(model); HL_OWNER.set(model, m); }
  return m;
}

/** 高亮第 idx 个关节（0 基）；传 -1 / null 取消全部高亮。 */
export function highlightJoint(idx) {
  for (const [mat, hex] of hlMats) {
    try { mat.emissive.setHex(hex); } catch (e) { /* 忽略 */ }
  }
  hlMats.clear();
  if (idx == null || idx < 0) return;
  const m = official || robot;
  if (!m) return;
  ownerMapOf(m).forEach((jointIdx, mesh) => {
    if (jointIdx !== idx) return;
    const mats = Array.isArray(mesh.material) ? mesh.material : [mesh.material];
    for (const mat of mats) {
      if (!mat || !mat.emissive || hlMats.has(mat)) continue;
      hlMats.set(mat, mat.emissive.getHex());
      mat.emissive.setHex(HL_COLOR);
    }
  });
}

/**
 * 构建"官方数模可动版"：把官方 GLB 的每个实体零件按 DH 关节轴挂到运动链上。
 *
 * 前提（已逐项核对）：
 *  1. GLB 由官方 STEP（AP214 装配体）转换而来，根节点带 rotX(-90°)+scale(0.001)，
 *     内部子零件顶点就是机器人基座坐标系（Z 向上, mm），与 DH 零位一致。
 *  2. 与 robotModel.buildRobot 使用同一条 DH 链，关节绕各自局部 Z 旋转。
 *  3. Object3D.attach() 自动补偿世界变换，模型外观零偏移但从此随关节运动。
 *     ★ 零件必须仍挂在 GLB 原始父级下（即传原件、不 clone 单 mesh），
 *       attach 的"源父级矩阵"才包含 mm→m 的 scale(0.001)。
 */
function buildArticulatedOfficial(parts, dh, mounting) {
  const J = dh.joints;
  const d1 = Math.abs(J[0].d), a1 = Math.abs(J[0].a);
  const a2 = Math.abs(J[1].a), a3 = Math.abs(J[2].a);
  const d4 = Math.abs(J[3].d);
  const zJ2 = d1;
  const zJ3 = d1 + a2;
  const zJ4 = zJ3 + a3;
  const xJ4 = a1;
  const xJ5 = a1 + d4;
  // ★ P1-E13：原 const xJ6 = xJ5 + d6 从未被读（零件归类只用到 xJ5，见下），
  //   连带 d6 也是死变量 —— 一并删掉，免得下次有人改 xJ5 时以为 J6 那段还在生效。

  const info = parts.map((m) => {
    m.geometry.computeBoundingBox();
    const bb = m.geometry.boundingBox;
    return {
      mesh: m,
      name: fixGbkName((m.name || "").split("^")[0]),
      c: bb.getCenter(new THREE.Vector3()),
      s: bb.getSize(new THREE.Vector3()),
      maxZ: bb.max.z,
      maxDim: Math.max(bb.max.x - bb.min.x, bb.max.y - bb.min.y, bb.max.z - bb.min.z),
      bmin: bb.min.clone(),   // 基座坐标系 mm（用于实测连杆外廓，如 EFORT 标识定位）
      bmax: bb.max.clone(),
    };
  });

  function classify(p) {
    if (p.maxDim > 900) return -1;
    const byName = NAME_TO_LINK[p.name];
    if (byName !== undefined) return byName;
    if (p.maxZ <= zJ2 * 0.70) return 0;
    if (p.maxZ <= zJ2 * 1.25) return 1;
    if (p.c.x >= xJ5 - 20) return p.c.x >= xJ5 + 40 ? 6 : 5;
    if (p.c.z < zJ3) return 2;
    if (p.c.z < zJ4 && p.c.x < xJ4 + 120) return 3;
    return 4;
  }

  const root = new THREE.Group();
  root.name = "official-articulated";
  root.rotation.x = -Math.PI / 2;
  if (mounting === "ceiling") root.rotation.z = Math.PI;
  else if (mounting === "wall") root.rotation.z = Math.PI / 2;
  root.scale.setScalar(0.001);
  // ★★ 抬升（root.position.y）**绝不能在这里设**：Object3D.attach() 的语义是
  //    "保持对象世界变换不变"，它会把 root 当时的 matrixWorld 一并补偿进零件的局部矩阵。
  //    提前抬升 → 补偿里多出 −PLATFORM_H → 抬升被完全抵消，模型实际仍停在 y=0
  //    （下沉进 180mm 高的站台里，看上去就是"基座穿插 / 散架"），
  //    而 root.position.y 读起来却是"已经抬好了"，极难排查。
  //    正确做法：所有 attach() 做完之后**再**设 root.position.y（见本函数末尾）。

  const joints = [];
  let parent = root;
  for (let i = 0; i < J.length; i++) {
    const j = J[i];
    const jg = new THREE.Group(); jg.name = "J" + (i + 1);
    parent.add(jg);
    const dz = new THREE.Group(); dz.position.z = j.d; jg.add(dz);
    const ax = new THREE.Group(); ax.position.x = j.a; dz.add(ax);
    const rx = new THREE.Group(); rx.rotation.x = deg2rad(j.alpha); ax.add(rx);
    joints.push(jg);
    parent = rx;
  }
  const tcpNode = new THREE.Group();
  tcpNode.name = "TCP";
  parent.add(tcpNode);

  const ee = buildEndEffector();
  tcpNode.add(ee.group);
  // ★ 工件锚点：挂在与末端工具**同一个**法兰节点上（官方链局部数值用 mm）
  attachPieceAnchor(tcpNode, 1);

  function applyJoints(qDeg) {
    for (let i = 0; i < joints.length; i++) {
      const off = (J[i] && J[i].theta_offset) || 0;
      joints[i].rotation.z = deg2rad((qDeg && qDeg[i] ? qDeg[i] : 0) + off);
    }
  }

  scene.add(root);
  root.visible = false;
  applyJoints([0, 0, 0, 0, 0, 0]);
  root.updateMatrixWorld(true);

  // nodes[0]=root(底座)  nodes[1..6]=J1..J6 连杆
  const nodes = [root, joints[0], joints[1], joints[2], joints[3], joints[4], joints[5]];
  const report = [];
  const linkBox = {};      // 连杆号 -> {min, max}（基座坐标 mm），由零件几何实测
  info.forEach((p) => {
    const k = classify(p);
    if (k < 0) {
      p.mesh.removeFromParent();
      p.mesh.visible = false;
      report.push(`${p.name || "(无名)"}: 参考件→隐藏`);
      return;
    }
    p.mesh.material = skinOf(p);
    nodes[k].attach(p.mesh);
    const b = linkBox[k] || (linkBox[k] = { min: p.bmin.clone(), max: p.bmax.clone() });
    b.min.min(p.bmin);
    b.max.max(p.bmax);
    report.push(`${p.name || "(无名)"}: L${k}`);
  });

  // EFORT 标识 → 挂在 **J4 连杆（小臂/手腕体）** 的 ±Y 两侧面(上/下)，位置由该连杆实测包围盒算出
  const L4 = linkBox[4];
  if (L4) {
    const sx = L4.max.x - L4.min.x;
    const syH = L4.max.y - L4.min.y;
    // X 略偏右(+X, 朝腕部方向), 不再死居中; 偏移夹在 [半个字宽, 30% 连杆长] 上限 60mm
    const cx = (L4.min.x + L4.max.x) / 2 + Math.min(sx * 0.3, 60);   // X 偏右
    const cz = (L4.min.z + L4.max.z) / 2;   // Z(深度) 中点 → 文字沿前臂铺在中央
    const w = Math.min(200, Math.max(80, sx * 0.5));
    const h = Math.min(48, Math.max(26, syH * 0.45));
    const yTop = L4.max.y + 1.5;            // 略浮出 +Y 上面
    const yBot = L4.min.y - 1.5;            // 略浮出 -Y 下面
    const logo = buildArmLogo(w, h, cx, cz, yTop, yBot);
    root.add(logo);
    root.updateMatrixWorld(true);
    nodes[4].attach(logo);    // J4 连杆 = 小臂/手腕体
  } else {
    console.warn("[官方数模] 未找到 J4 连杆零件，跳过 EFORT 标识");
  }

  // ★★ 抬升放在**所有 attach() 之后**：此刻零件的世界坐标已被 attach 固定成 GLB 原始坐标
  //   （底面 y=0），再改 root.position.y 才能把整机一起平移到站台台面 —— 包括链条上的
  //   末端夹爪（tcpNode 子节点）与已 attach 的 EFORT 标识，全部同步上抬。
  //   落地安装抬 PLATFORM_H；吊装/侧装不抬（整体已旋转，贴装面本来就在 0）。
  if (mounting !== "ceiling" && mounting !== "wall") root.position.y = PLATFORM_H;
  root.updateMatrixWorld(true);

  // ---- 装配自检 ----
  // 只量**官方零件**的合并包围盒（不含末端夹爪/EFORT 标识，避免干扰判据）。
  // 官方数模实测：高 0.810 / 前后 0.688 / 左右 0.256 m，底面 z=0（落地时世界 y = 台面）。
  // ★ 校验不过就**抛错并把自己从场景摘掉** → loadOfficialModel 的 catch 回退程序化模型。
  //   直接把挂错的模型显示出来 = 用户看到"散架"，而且比回退更难排查。
  root.updateMatrixWorld(true);
  const pb = new THREE.Box3();
  let pbCount = 0;
  info.forEach((p) => {
    if (!p.mesh.parent) return;              // 参考件已摘除
    pb.union(new THREE.Box3().setFromObject(p.mesh));
    pbCount += 1;
  });
  const ph = pb.max.y - pb.min.y;
  const px = pb.max.x - pb.min.x;
  const pz = pb.max.z - pb.min.z;
  // 吊装/侧装整体被旋转，长宽高互换 → 判据不适用，只做落地安装的校验。
  if (mounting !== "ceiling" && mounting !== "wall") {
    const bad = [];
    if (ph < 0.70 || ph > 0.92) bad.push(`高 ${ph.toFixed(3)}m(应≈0.810)`);
    if (px < 0.55 || px > 0.85) bad.push(`前后跨度 ${px.toFixed(3)}m(应≈0.688)`);
    if (pz < 0.15 || pz > 0.40) bad.push(`左右跨度 ${pz.toFixed(3)}m(应≈0.256)`);
    if (Math.abs(pb.min.y - PLATFORM_H) > 0.03) {
      bad.push(`底面 y ${pb.min.y.toFixed(3)}(应≈台面 ${PLATFORM_H})`);
    }
    if (bad.length) {
      scene.remove(root);      // 摘掉半成品，别留在场景里当"散架"的模型
      disposeTree(root, cachedTextures());   // 连带回收它的几何/材质，避免残留占显存
      throw new Error(`[官方数模] 装配自检失败 → 回退程序化模型：${bad.join("；")}。零件归类 ${report.join(" | ")}`);
    }
  }

  console.info(
    "[官方数模] 已按 DH 关节轴重挂 %d 个零件(高 %.3fm/前后 %.3fm/左右 %.3fm) → %s",
    pbCount, ph, px, pz, report.join(" | "),
  );

  function getTcp() {
    root.updateMatrixWorld(true);
    const v = new THREE.Vector3();
    tcpNode.getWorldPosition(v);
    return { x: v.x * 1000, y: v.y * 1000, z: v.z * 1000 };
  }

  return { root, applyJoints, getTcp, joints };
}

/** 官方数模（GLB）→ 六轴可动版。**直接用原件，不 clone**（份数=1）。 */
export async function loadOfficialModel(url = "/models/robot_full.glb") {
  const dh = activeDh || FALLBACK_DH;
  const loader = new GLTFLoader();
  try {
    const gltf = await loader.loadAsync(url);
    gltf.scene.updateMatrixWorld(true);
    const src = gltf.scene.getObjectByName("ER8-700H") || gltf.scene.children[0] || gltf.scene;
    const parts = [];
    src.traverse((o) => { if (o.isMesh && o.geometry) parts.push(o); });
    if (!parts.length) throw new Error("GLB 中没有可用的网格零件");
    // 幂等：重复调用时先移除旧模型，避免场景里残留第二台机器人
    if (official) {
      const old = official.root;
      scene.remove(old);
      // ★ 只 remove 不 dispose 会把几何/材质留在 GPU 里（GLB 零件 + 夹爪 + 标识），
      //   反复切换模型几次就把显存吃满 → 3D 变黑。这里连资源一起回收。
      disposeTree(old, cachedTextures());
      official = null;
    }
    official = buildArticulatedOfficial(parts, dh, activeMounting);
  } catch (e) {
    console.warn("官方数模加载失败, 回退程序化模型:", e);
    official = null;
  }
  applyAppearance();
}

/**
 * 外观：**固定用官方数模**（可动版）；官方数模加载失败时回退程序化模型。
 * ★ 以前这里有一套"程序化 / 官方"切换开关，现已移除 —— 两套模型只是同一姿态的
 *   两种画法，留着切换只会让"我看到的到底是哪一具"变成现场排查的噪音。
 * ★ 无论显示哪一具，姿态都由 pushPose 同步驱动，不会因回退而丢姿态。
 */
function applyAppearance() {
  const officialOn = !!official;
  if (official) official.root.visible = officialOn;
  if (robot) robot.root.visible = !officialOn;
  pushPose(qCurrent);   // 换模型后立刻把当前姿态补上，避免"切换瞬间姿态丢失"
}

// ---------------- 相机视角（保存 / 复位） ----------------
export function getCameraPose() {
  ensureScene();
  return {
    position: [camera.position.x, camera.position.y, camera.position.z],
    target: [controls.target.x, controls.target.y, controls.target.z],
  };
}

export function applyCameraPose(position, target) {
  ensureScene();
  if (Array.isArray(position) && position.length === 3) camera.position.set(...position);
  if (Array.isArray(target) && target.length === 3) controls.target.set(...target);
  controls.update();
}

// =====================================================================
// 资源释放
// ★ 为什么必须显式释放：切官方模型/整页销毁时若只 scene.remove()，几何与材质
//   仍留在 GPU 里（three.js 不会自动回收）。反复切换几次，显存就被吃光，
//   表现就是"用着用着 3D 变黑"。这里给出统一出口。
// =====================================================================

/**
 * 遍历子树释放 geometry / material / 贴图。
 * @param {THREE.Object3D} root
 * @param {Set} keepTextures 需要保留的贴图（模块级缓存、跨模型复用）
 */
function disposeTree(root, keepTextures) {
  if (!root) return;
  const mapKeys = ["map", "normalMap", "roughnessMap", "metalnessMap",
    "emissiveMap", "aoMap", "alphaMap", "bumpMap", "displacementMap"];
  root.traverse((o) => {
    if (o.geometry && o.geometry.dispose) {
      try { o.geometry.dispose(); } catch (e) { /* 忽略 */ }
    }
    const mats = Array.isArray(o.material) ? o.material : (o.material ? [o.material] : []);
    mats.forEach((m) => {
      if (!m) return;
      mapKeys.forEach((k) => {
        const t = m[k];
        if (!t || !t.dispose) return;
        if (keepTextures && keepTextures.has(t)) return;   // 缓存贴图，别误杀
        try { t.dispose(); } catch (e) { /* 忽略 */ }
      });
      if (m.dispose) { try { m.dispose(); } catch (e) { /* 忽略 */ } }
    });
  });
}

/** 模块级缓存贴图集合：这些跨模型复用，释放时必须跳过。 */
function cachedTextures() {
  const s = new Set();
  if (logoTex) s.add(logoTex);
  if (envTex) s.add(envTex);
  return s;
}

/**
 * 释放全部 3D 资源（根组件卸载时调用）。
 * ★ 只动前端渲染层：不触碰机器人链路、速度锁定、J1-J5 锁定。
 * 调用后所有单例置空，若再次 ensureScene() 会重新建一整套。
 */
export function disposeAll() {
  // 审计修复 P0-ui-3：撤掉切视图遗留的尺寸补救 rAF，避免单例置空后仍去渲染
  if (sizeFixRaf) { cancelAnimationFrame(sizeFixRaf); sizeFixRaf = 0; }
  // 1) 各子模块自清（从场景摘除自己的 group，并释放自有资源）
  [sim, fence, ghost, ghostB, cell, sorting].forEach((m) => {
    try { if (m && m.dispose) m.dispose(); } catch (e) { /* 忽略 */ }
  });
  // 2) 兜底清扫场景内残留的几何/材质/贴图
  if (scene) {
    disposeTree(scene, cachedTextures());
    try { scene.clear(); } catch (e) { /* 忽略 */ }
  }
  // 3) 控制器 / 环境贴图 / 渲染器与画布
  try { if (controls) controls.dispose(); } catch (e) { /* 忽略 */ }
  try { if (envTex) envTex.dispose(); } catch (e) { /* 忽略 */ }
  if (renderer) {
    const cvs = renderer.domElement;
    try { renderer.dispose(); } catch (e) { /* 忽略 */ }
    // forceContextLoss 主动让浏览器回收这块 WebGL 上下文 ——
    // 不做的话反复挂载/卸载页面会攒下多个上下文，最终触发"上下文数超限"整页黑屏。
    try { renderer.forceContextLoss(); } catch (e) { /* 忽略 */ }
    if (cvs && cvs.parentElement) cvs.parentElement.removeChild(cvs);
  }
  // 4) 单例引用全部置空
  renderer = null; scene = null; camera = null; controls = null; envTex = null;
  robot = null; official = null; fence = null; cell = null;
  ghost = null; ghostB = null; sim = null; activeDh = null;
  // ★ 工件锚点必须一起置空：它挂在被 dispose 的模型节点上，
  //   不置空就会指向"已从场景摘掉的旧节点"—— 重挂后料会贴在旧位置上。
  eeAnchor = null;
  sorting = null;
  qReady = false; lastFrameMs = 0; lastSafetyKey = "";
  lastPixelRatio = 0;
  glState = "ok";
  hlMats.clear();
  Object.keys(containers).forEach((k) => { delete containers[k]; });
}
