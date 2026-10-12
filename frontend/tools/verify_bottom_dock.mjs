// =====================================================================
// 底部状态区守卫（headless 静态检查）
//
// 用户口径（2026-10-11，原话）：
//   "底部的真机链路能不能跟机器人/摄像头/示教器那样样式…还有运行日志也放进去，
//    五个样式、状态，五选一显示，点别的地方自己隐藏，默认运行日志开启状态，
//    绿色/黄色/红色点要亮起来，运行日志放在最左侧。"
// 用户口径（2026-10-11 第二轮，原话）：
//   "底部有两个真机链路两个合并掉" —— 原「机器人 真实链路」片与「真机链路 就绪」片
//   合并为一个「真机链路」片（保留 RcReadyCard，机器人链路内容迁入其面板）。
//
// 这个脚本把这次改版里**最容易悄悄退化**的几条钉死：
//   A. 状态片的名字与顺序（运行日志最左、真机链路最右；机器人片已被合并掉）；
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

// ---- 摄像头/示教器两片沿用 linkview.js 的既有样式与判据（机器人片已并入真机链路）----
const { allPills } = await import(pathToFileURL(join(root, "src", "linkview.js")).href);
const pillLabels = allPills(null).map((p) => p.label);
// ★ 2026-10-11 第二轮用户口径：「底部有两个真机链路两个合并掉」——
//   原「机器人 真实链路」片 + 「真机链路 就绪」片合并为一个「真机链路」片
//   （保留内容更全的 RcReadyCard，并把机器人链路内容迁入其面板）。
//   → StatusStrip 实际渲染的片 = 运行日志 / 摄像头 / 示教器 / 真机链路。
const EXPECT = ["运行日志", "摄像头", "示教器", "真机链路"];

console.log("");
console.log("== A. 状态片的名字与顺序（运行日志最左、真机链路最右；机器人已并入真机链路） ==");
{
  ok("linkview 三片仍是 机器人/摄像头/示教器（承上，纯函数不改）",
     JSON.stringify(pillLabels) === JSON.stringify(["机器人", "摄像头", "示教器"]),
     JSON.stringify(pillLabels));
  ok("StatusStrip 定义了「运行日志」片", strip.includes('label: "运行日志"'));
  ok("StatusStrip 定义了「真机链路」片", strip.includes('label: "真机链路"'));
  // ★ 两个「真机链路」合并：tabs 必须**过滤掉** robot 片（否则底部又出现两个真机链路入口）。
  ok("★ tabs = [logTab, ...link.pills.filter(key!=='robot'), linkTab]（机器人片已并入）",
     /tabs\s*=\s*computed\(\s*\(\)\s*=>\s*\[[\s\S]{0,400}?link\.pills\.filter\(\(p\)\s*=>\s*p\.key\s*!==\s*"robot"\)[\s\S]{0,240}?linkTab\.value/.test(strip));
  ok("★ 真机链路面板内含「机器人链路」块（原机器人片内容迁入，不丢功能）",
     strip.includes("机器人链路") && strip.includes('class="lp-robot"'));
  ok(`期望顺序 ${EXPECT.join(" · ")}`, EXPECT.length === 4);
  // 多选一：同一个展开位（open === p.key），再点同一片收起（toggle 里 next==="" 的分支）
  ok("★ 多选一门控 = open === p.key（共用同一个浮层容器）", strip.includes("open === p.key"));
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
console.log("== E. 运行日志「来新消息就自动到底」（水位 = logSeq，不是 log.length） ==");
{
  const exec = read("src/stores/exec.js");
  // ★★ 用户口径（2026-10-11）：「添加日志要自动最底部，根据新的消息」。
  //   踩过的坑：滚底的 watch 绑的是 `exec.log.length`，而 `logLine()` 超过 LOG_MAX(240)
  //   会 splice 掉旧行 → 长度恒为 240、不再变化 → 水位失效（恰恰日志越长越需要跟底）。
  ok("★ exec 有单调递增的 `logSeq`（并说明为什么不能用 log.length）",
     /logSeq:\s*0/.test(exec) && /logSeq \+= 1/.test(exec));
  ok("★ logLine 里 logSeq 递增；clearLog 归零",
     /this\.logSeq \+= 1/.test(exec) && /clearLog\(\)\s*\{\s*this\.log = \[\];\s*this\.logSeq = 0;/.test(exec));
  ok("★ 滚底 watch 绑 `logSeq`（不再绑 `log.length` —— 满 240 行后长度就不再变化）",
     /watch\(\(\)\s*=>\s*exec\.logSeq[\s\S]{0,120}logScrollToBottom\(\)/.test(strip)
     && !/watch\(\(\)\s*=>\s*exec\.log\.length/.test(strip));
  ok("★ 「粘底」语义保留：用户往上翻看历史时不抢滚动条",
     /logStick/.test(strip) && /LOG_BOTTOM_EPS\s*=\s*24/.test(strip)
     && /onLogScroll/.test(strip));
  ok("★ 点开「运行日志」片 → 重置为粘底并滚到底",
     /watch\(open[\s\S]{0,120}logStick\.value = true;[\s\S]{0,80}logScrollToBottom\(\)/.test(strip));
}

console.log("");
console.log("== F. 「数据过期」水位改外部时钟（防假绿）+ 未知不得报成已知 ==");
{
  const linkJs = read("src/stores/link.js");
  const rcJs = read("src/stores/rcReady.js");
  const appVue = read("src/App.vue");
  // ★★ 2026-10-11：`stale` 原来靠"收到数据"推进水位 —— 掉线时 link 会停轮询、控制器 WS 帧
  //   也会停 → 水位**冻结** → "过期"永远算不出来 → 底栏挂"旧绿"（最危险的假正常）。
  //   现在由 App 的 1s 时钟（与令牌到期扫描同一个）**无条件**推进。
  ok("★ link 有外部时钟 `beat()`，且 App 的 1s 时钟无条件调用",
     /beat\(\)\s*\{\s*this\.tick = Date\.now\(\)/.test(linkJs) && /link\.beat\(\)/.test(appVue));
  ok("★ rcReady 也有 `beat()`，且 `stale` 用 `clk` 而**不是** `Date.now()`",
     /beat\(\)\s*\{\s*this\.clk = Date\.now\(\)/.test(rcJs)
     && /stale: \(s\) => \(s\.snap \? \(\(s\.clk - s\.wsAt\) > RC_STALE_MS\) : true\)/.test(rcJs)
     && !/stale: \(s\) => \(s\.snap \? \(Date\.now\(\)/.test(rcJs));
  ok("★ 控制器快照过期 → 底栏**绝不报绿**（压黄并写「数据过期」）",
     /if \(rc\.stale\) return "warn"/.test(strip) && /rc\.stale \? "数据过期"/.test(strip));
  // ★★ `servoOn === null`（后端未返回）是三元的 falsy —— 原写法会把"不知道"说成"未上电"。
  //   现在必须是**三态**：null → 「未知」；true → 「未就绪」；false → 「未上电」。
  ok("★ `servoOn === null` → 报「未知」（三态：未知 / 未就绪 / 未上电，不再把未知说成未上电）",
     /rc\.servoOn === null \? "未知"/.test(strip)
     && /rc\.servoOn \? "未就绪" : "未上电"/.test(strip)
     && /readyValue = !rc\.ok \? "读不到"\s*\n\s*: rc\.stale \? "数据过期"/.test(strip));
}

console.log("");
console.log(`结果: ${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);
