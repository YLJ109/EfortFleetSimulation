// =====================================================================
// 底部状态区"五选一"守卫（headless 静态检查）
//
// 用户口径（2026-10-11，原话）：
//   "底部的真机链路能不能跟机器人/摄像头/示教器那样样式…还有运行日志也放进去，
//    五个样式、状态，五选一显示，点别的地方自己隐藏，默认运行日志开启状态，
//    绿色/黄色/红色点要亮起来，运行日志放在最左侧。"
//
// 这个脚本把这次改版里**最容易悄悄退化**的几条钉死：
//   A. 五个片的名字与顺序（运行日志在最左）；
//   B. 默认开运行日志；运行中自动开日志；
//   C. 点外部收起（document 监听 + #bottom-zone 判定），且不阻塞页面；
//   D. 灯的三态判据存在，且只按真实字段取色（不兜底成绿）。
//
// 运行： cd frontend && node tools/verify_bottom_dock.mjs
// =====================================================================
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (rel) => readFileSync(join(root, rel), "utf8");

let pass = 0, fail = 0;
function ok(name, cond, extra = "") {
  if (cond) { pass++; console.log("  PASS  " + name); }
  else { fail++; console.log("  FAIL  " + name + (extra ? "   → " + extra : "")); }
}

const strip = read("src/components/StatusStrip.vue");
const ui = read("src/stores/ui.js");
const s3d = read("src/stores/sorting3d.js");

// ---- 前四片沿用 linkview.js 的既有样式与判据（三片 = 机器人/摄像头/示教器）----
const { allPills } = await import(pathToFileURL(join(root, "src", "linkview.js")).href);
const pillLabels = allPills(null).map((p) => p.label);
const EXPECT = ["运行日志", ...pillLabels, "真机链路"];

console.log("");
console.log("== A. 五个片的名字与顺序（运行日志最左） ==");
{
  ok("linkview 三片是 机器人/摄像头/示教器（承上）",
     JSON.stringify(pillLabels) === JSON.stringify(["机器人", "摄像头", "示教器"]),
     JSON.stringify(pillLabels));
  ok("StatusStrip 定义了「运行日志」片", strip.includes('label: "运行日志"'));
  ok("StatusStrip 定义了「真机链路」片", strip.includes('label: "真机链路"'));
  // 五片的拼装顺序：日志（最左）→ link.pills（机器人/摄像头/示教器）→ 真机链路（最右）
  ok("★ tabs 顺序 = [logTab, ...link.pills, linkTab]（运行日志最左、真机链路最右）",
     /tabs\s*=\s*computed\(\s*\(\)\s*=>\s*\[logTab\.value\s*,\s*\.\.\.link\.pills\s*,\s*linkTab\.value\]\s*\)/.test(strip));
  ok(`期望顺序 ${EXPECT.join(" · ")}`, EXPECT.length === 5);
  // 五选一：同一个展开位（open === p.key），再点同一片收起（toggle 里 next==="" 的分支）
  ok("★ 五选一门控 = open === p.key（共用同一个浮层容器）", strip.includes("open === p.key"));
  ok("★ 再点同一片可收起（toggle：next = open===k ? '' : k）",
     /open\.value\s*===\s*k\s*\?\s*""\s*:\s*k/.test(strip));
}

console.log("");
console.log("== B. 默认开日志 + 运行中自动开日志 ==");
{
  ok("★ ui.statusPopover 默认值 = \"log\"（进入页面默认开运行日志）",
     /statusPopover:\s*"log"/.test(ui), "未找到默认值");
  ok("★ 有执行在跑 = exec.running || s3d.running",
     /anyRunning\s*=\s*computed\(\s*\(\)\s*=>\s*exec\.running\s*\|\|\s*s3d\.running\s*\)/.test(strip));
  ok("★ watch(anyRunning) 运行中自动开日志（setStatusPopover(\"log\")）",
     /watch\(anyRunning[\s\S]{0,160}setStatusPopover\("log"\)/.test(strip));
  ok("★ 运行中不许整体隐藏（statusPopover 变空且 running → 重新打开 log）",
     /watch\(\(\)\s*=>\s*ui\.statusPopover[\s\S]{0,160}anyRunning\.value[\s\S]{0,80}setStatusPopover\("log"\)/.test(strip));
  ok("★ sorting3d store 暴露 running（供全局判据用）",
     /running:\s*false/.test(s3d) && /this\.running\s*=\s*!!d\.running/.test(s3d));
  ok("★ scene-state 轮询不新增轮询者（复用 sorting3d.tick 的既有请求）",
     /apiControl\("\/control\/scene-state"\)/.test(s3d));
  ok("★ Esc 语义保护：log 不算弹层（ui.popoverModal 排除 log）",
     /popoverModal:\s*\(s\)\s*=>[\s\S]{0,80}!==\s*"log"/.test(ui));
}

console.log("");
console.log("== C. 点外部收起（非阻塞） ==");
{
  ok("★ 用 document mousedown 监听点击外部", /addEventListener\("mousedown"/.test(strip));
  ok("★ 「状态区以内」判定用 #bottom-zone（点控制区不误收）",
     strip.includes('closest("#bottom-zone")'));
  ok("★ 收起走 close()（运行中会自动回到 log）", /function close\(\)/.test(strip));
  ok("★ 没有铺全屏遮罩（非阻塞，页面照常可点）",
     !/link-backdrop/.test(strip));
}

console.log("");
console.log("== D. 灯的三态判据（按真实字段，不兜底成绿） ==");
{
  // 四档色调类齐备（ok/warn/err/off），与 linkview 同一套
  for (const t of ["ok", "warn", "err", "off"]) {
    ok(`状态片有 .link-pill.${t} 配色`, new RegExp(`\\.link-pill\\.${t}\\b`).test(strip));
  }
  ok("★ 运行日志灯 = 最新一条日志级别（err→红 / warn→黄 / 其余→绿 / 空→灰）",
     /last\.level\s*===\s*"err"/.test(strip) && /last\.level\s*===\s*"warn"/.test(strip)
     && /!last\s*\?\s*"off"/.test(strip));
  ok("★ 真机链路灯取自 rcReady 真实寄存器字段",
     /rc\.ok/.test(strip) && /rc\.bits/.test(strip) && /rc\.joggable/.test(strip) && /rc\.ready/.test(strip));
  ok("★ 真机链路：报警/急停归红", /b\.alarm\s*\|\|\s*b\.estop/.test(strip));
  ok("★ 真机链路：非 AUTO/远程档归红", /!\s*rc\.joggable\)\s*return\s*"err"/.test(strip));
  ok("★ 日志面板复用底栏日志观感（等宽 + 按级别配色）",
     /\.lp-log-body/.test(strip) && /\.lp-log-line\.err/.test(strip));
  ok("★ 真机链路片完整复用 RcReadyCard（内容未删）",
     /import RcReadyCard from "\.\/RcReadyCard\.vue"/.test(strip) && strip.includes("<RcReadyCard"));
}

console.log("");
console.log(`结果: ${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);
