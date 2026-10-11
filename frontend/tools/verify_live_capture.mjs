// =====================================================================
// 「相机 / 实时捕捉」口径守卫（headless 静态检查）
//
// 用户口径（2026-10-11，原话两条，以后者为准）：
//   ① "启动项目时要开启实时捕捉，真实监控不需要权限，相机也要用户自己开启（不需要权限），
//      如果拿到权限了自动实时捕捉，并自动开启相机"
//   ② "相机个实时捕捉默认关闭的状态"
//
// 最终口径：
//   · 相机：默认关（不再"启动即开相机"）；
//   · 实时捕捉：默认关，是「真实监控」页上的显式开关；关掉时**真正停流**（不是只隐藏）；
//   · 两者都不需要控制权限即可手动开关（按钮不含 disabled）；
//   · 拿到控制权限（上升沿）→ 自动开「实时捕捉」+ 自动开「相机」（幂等，不重复发）。
//
// 这个脚本把最容易悄悄退化的几条钉死：一旦有人把"默认关"改回"启动自动开"，
// 或把权限自动开启弄丢，这里立刻变红。
//
// 运行： cd frontend && node tools/verify_live_capture.mjs
// =====================================================================
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (rel) => readFileSync(join(root, rel), "utf8");

let pass = 0, fail = 0;
function ok(name, cond, extra = "") {
  if (cond) { pass++; console.log("  PASS  " + name); }
  else { fail++; console.log("  FAIL  " + name + (extra ? "   → " + extra : "")); }
}

const app = read("src/App.vue");
const cam = read("src/stores/camera.js");
const panel = read("src/components/CameraPanel.vue");
const live = read("src/components/RealMonitor.vue");
const strip = read("src/components/StatusStrip.vue");

// ---------------------------------------------------------------------
console.log("");
console.log("== A. 默认关：相机与「实时捕捉」都不在启动时自动开 ==");
{
  ok("A1 camera store 的 liveCapture 默认 false（实时捕捉默认关）",
     /liveCapture:\s*false/.test(cam));
  // 旧实现是一句 cam.refresh().then(() => { ... cam.open() })。
  // ★ 不能按"是否出现『自动打开摄像头』这几个字"判定 —— 新口径的注释里会写
  //   “不再自动打开摄像头”，仍然包含这几个字（会误报）。所以改判**生效口径**：
  //   文件里不再直接调 cam.open()，且写明了"默认都关"的新口径。
  ok("A2 App.vue 写明相机/实时捕捉『默认都关』的新口径",
     app.includes("默认都关"));
  ok("A3 App.vue 整个文件都不再直接调 cam.open()（启动不再自动开相机）",
     !/cam\.open\s*\(/.test(app));
  ok("A4 App.vue 不再有 refresh().then(...open) 那种启动自动开相机写法",
     !/cam\.refresh\(\)\.then\(/.test(app));
}

// ---------------------------------------------------------------------
console.log("");
console.log("== B. 「实时捕捉」开关：真实监控页 · 默认关 · 手动可开 · 不需权限 ==");
{
  ok("B1 RealMonitor 渲染「实时捕捉」开关卡片", live.includes("实时捕捉"));
  ok("B2 开关动作 = cam.toggleLiveCapture()（手动开/关）",
     live.includes("cam.toggleLiveCapture()"));
  ok("B3 状态如实绑定 cam.liveCapture（不是另算一份）",
     live.includes("cam.liveCapture"));
  const btn = /<button[^>]*@click="cam\.toggleLiveCapture\(\)"[^>]*>/s.exec(live);
  ok("B4 找到「实时捕捉」按钮元素", !!btn);
  ok("B5 按钮**不绑定** disabled（不需要控制权限也能手动开）",
     !!btn && !/disabled/.test(btn[0]));
  ok("B6 RealMonitor 不引入 auth store（实时捕捉与权限无关）",
     !/useAuthStore/.test(live));
  ok("B7 RealMonitor 不引用 controlActive（未误绑控制权限）",
     !live.includes("controlActive"));
}

// ---------------------------------------------------------------------
console.log("");
console.log("== C. 权限到手 → 自动开「实时捕捉」+ 自动开「相机」（幂等） ==");
{
  ok("C1 App.vue watch 控制权限字段 auth.controlActive",
     /watch\(\(\) => auth\.controlActive/.test(app));
  ok("C2 只在上升沿动作（!now || was → return）",
     /if\s*\(\s*!now\s*\|\|\s*was\s*\)\s*return/.test(app));
  ok("C3 自动开「实时捕捉」= cam.setLiveCapture(true)",
     app.includes("cam.setLiveCapture(true)"));
  ok("C4 自动开「相机」= cam.ensureOpen()（幂等：已开/过渡中直接返回）",
     app.includes("cam.ensureOpen()"));
  ok("C5 幂等实现：ensureOpen 首行判断 opened || busy 直接返回",
     /if\s*\(\s*this\.opened\s*\|\|\s*this\.busy\s*\)\s*return/.test(cam));
  ok("C6 immediate:true（刷新时本地已存有效令牌也能自动开）",
     /watch\(\(\) => auth\.controlActive[\s\S]{0,400}?immediate:\s*true/.test(app));
}

// ---------------------------------------------------------------------
console.log("");
console.log("== D. 关掉「实时捕捉」= 真正停流（不是只隐藏画面） ==");
{
  ok("D1 CameraPanel 取流门控里含 cam.liveCapture",
     /const\s+allowCapture\s*=[\s\S]{0,140}?cam\.liveCapture/.test(panel));
  ok("D2 门控只约束「真实监控」页（OWN_VIEW !== \"live\" 不受影响）",
     /OWN_VIEW\s*!==\s*["']live["']/.test(panel));
  // want 为假时走 else → releaseStream()（移除两路 <img> 的 src，真正停流）
  ok("D3 want 为假时走 releaseStream()（真停流，不是隐藏）",
     /const\s+want\s*=[\s\S]{0,600}?else\s*\{[\s\S]{0,200}?releaseStream\(\)/.test(panel));
  ok("D4 watch 同时监听 liveCapture 变化 → 重算取流",
     /watch\(\[[\s\S]{0,120}?cam\.liveCapture[\s\S]{0,120}?\],\s*connectStream\)/.test(panel));
  ok("D5 手动开相机会顺带打开实时捕捉（避免开了相机却没画面）",
     /if\s*\(!cam\.opened\)\s*cam\.setLiveCapture\(true\)/.test(panel));
  ok("D6 底栏「打开相机」也补开实时捕捉（同一口径）",
     /doCamOpen[\s\S]{0,220}?cam\.setLiveCapture\(true\)/.test(strip));
}

// ---------------------------------------------------------------------
console.log("");
console.log("== E. 不删除既有能力：相机面板的手动开关/设备切换/取流链路都还在 ==");
{
  ok("E1 相机面板仍有「开启相机 / 关闭相机」开关（onToggle → cam.toggle）",
     panel.includes("cam.toggle()") && panel.includes("开启相机"));
  ok("E2 相机面板仍有设备切换",
     panel.includes("onDeviceChange") && panel.includes("switchDevice"));
  ok("E3 相机面板仍有截图 / 重连",
     panel.includes("onSnap") && panel.includes("reconnectCam"));
}

console.log("");
console.log("结果: " + pass + " 通过 / " + fail + " 失败");
process.exit(fail ? 1 : 0);
