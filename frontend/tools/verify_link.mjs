/**
 * headless 回归：底栏三盏常驻状态灯（src/linkview.js）
 *
 * 背景：用户要求「摄像头有没有连接、机器人有没有连接都要显示；示教器也要显示出来，
 *       不管有没有自动模式都要显示」。这类"按状态决定文案与颜色"的代码有一个
 *       非常危险的失效模式 —— **写错了界面上看着还是正常的**：
 *         · 后端少给一个字段 → 兜底分支静默生效，灯变成灰色/绿色，看不出是"没数据"；
 *         · 状态合并（如把"强制模拟"和"掉线降级"算成同一档）→ 真故障被长期噪声掩盖。
 *       所以把映射抽成纯函数，在这里逐档钉死 state → tone / label。
 *
 * 运行：node tools/verify_link.mjs     （已挂在 npm run verify 末尾）
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..");
const REPO = path.resolve(ROOT, "..");

// ★ Windows 下动态 import 必须走 file:// URL（"d:\..." 会被当成 scheme "d:"）
const {
  RUN_MODES, TONES, toneOf, robotPill, cameraPill, modePill,
  allPills, stripTone, worstPill, canClaim,
} = await import(pathToFileURL(path.join(ROOT, "src", "linkview.js")).href);

let pass = 0, fail = 0;
function ok(cond, msg) {
  if (cond) { pass++; console.log("  PASS  " + msg); }
  else { fail++; console.log("  FAIL  " + msg); }
}
function eq(actual, expected, msg) {
  const a = JSON.stringify(actual), b = JSON.stringify(expected);
  ok(a === b, `${msg}${a === b ? "" : `  (期望 ${b}，实际 ${a})`}`);
}
function section(t) { console.log("\n== " + t + " =="); }

/** 造一份 links 快照（字段名与后端 _robot_link/_camera_link/_links 对齐）。 */
function links({ robot, camera, run_mode } = {}) {
  return {
    robot: robot === undefined ? { level: "ok", state: "real", label: "真实链路", detail: "d", host: "1.2.3.4", port: 502 } : robot,
    camera: camera === undefined ? { level: "ok", state: "ready", label: "相机已连接", detail: "d", base: "http://127.0.0.1:8100" } : camera,
    run_mode: run_mode === undefined ? { level: "ok", mode: "T1", label: "T1 手动低速", desc: "d", confirmed: true, joggable: true } : run_mode,
  };
}

// ---------------------------------------------------------------------
section("A. id 与后端不漂移（跨语言常量守卫）");
// ---------------------------------------------------------------------
// 前端 RUN_MODES 的 id 必须与 backend/app/services/runmode.py 的 MODES 完全一致。
// 这类"两边各写一份常量"最容易在某次改动里只改一边，然后前端声明 T2、
// 后端收到 T2 却不在 MODES 里 → 400，而且只有点下去才发现。
const pyPath = path.join(REPO, "backend", "app", "services", "runmode.py");
ok(fs.existsSync(pyPath), "A1 找到后端 runmode.py");
const py = fs.readFileSync(pyPath, "utf8");
const m = py.match(/MODES\s*:\s*Tuple\[str,\s*\.\.\.\]\s*=\s*\(([^)]*)\)/);
ok(!!m, "A2 能在 runmode.py 里定位到 MODES 定义");
const pyModes = m ? m[1].split(",").map((s) => s.trim().replace(/^["']|["']$/g, "")).filter(Boolean) : [];
eq(RUN_MODES.map((o) => o.id), pyModes, "A3 前端档位 id 与后端 MODES 完全一致");
ok(RUN_MODES.every((o) => o.label && o.desc), "A4 每一档都有 label 与 desc（面板里要展示）");
eq(TONES, ["ok", "warn", "err", "off"], "A5 TONES 四个色调齐备（off = 拿不到状态）");

// ★ A6/A7：前端 desc 与后端 JOGGABLE 的**语义**不能互相矛盾。
//   真实踩过的坑：2026-09-23 实机确认"只有 AUTO/远程 档下上位机指令有效"（与官方
//   手册语义相反）之后，后端把 T1/T2 改成了拒绝点动，前端 desc 却还写着
//   "T1 示教档：可用，TCP ≤ 50 mm/s" —— 操作员按界面选 T1，然后对着
//   "写成功但机器人不动"排查半天。id 有漂移守卫，语义没有，这里补上。
const jm = py.match(/JOGGABLE\s*:\s*Dict\[str,\s*bool\]\s*=\s*\{([^}]*)\}/);
ok(!!jm, "A6 能在 runmode.py 里定位到 JOGGABLE 定义");
const joggable = {};
if (jm) {
  for (const part of jm[1].split(",")) {
    const kv = part.split(":");
    if (kv.length === 2) joggable[kv[0].trim().replace(/^["']|["']$/g, "")] = kv[1].trim() === "True";
  }
}
for (const o of RUN_MODES) {
  const can = joggable[o.id];
  const saysCan = o.desc.includes("可用") && !o.desc.includes("不可用");
  eq(saysCan, can === true,
    `A7 ${o.id} 前端 desc 与后端 JOGGABLE 一致（后端 joggable=${can}）`);
}

// ---------------------------------------------------------------------
section("B. level → tone 映射（未知等级绝不回退成绿）");
// ---------------------------------------------------------------------
eq(toneOf("ok"), "ok", "B1 ok → ok");
eq(toneOf("warn"), "warn", "B2 warn → warn");
eq(toneOf("err"), "err", "B3 err → err");
eq(toneOf("error"), "err", "B4 error → err（兼容别名）");
eq(toneOf("info"), "off", "B5 info → off（不是绿：info 不代表链路正常）");
eq(toneOf(""), "off", "B6 空 → off");
eq(toneOf(undefined), "off", "B7 undefined → off");
eq(toneOf("weird-new-level"), "off", "B8 ★ 未知等级 → off（灰），绝不能兜底成 ok 绿");

// ---------------------------------------------------------------------
section("C. 机器人灯：四态逐档钉死");
// ---------------------------------------------------------------------
// ★ 这四档必须分开：把 sim_forced 与 sim 合并，等于让"天天在模拟"掩盖真掉线。
eq(robotPill(links({ robot: { level: "ok", state: "real", label: "真实链路", detail: "d", host: "h", port: 502 } })).tone,
  "ok", "C1 real → 绿");
eq(robotPill(links({ robot: { level: "warn", state: "sim_forced", label: "强制模拟", detail: "simulate=always", simulate: "always" } })).tone,
  "warn", "C2 sim_forced → 黄（用户自己选的配置，不该红）");
eq(robotPill(links({ robot: { level: "err", state: "sim", label: "模拟数据（真机掉线降级）", detail: "d" } })).tone,
  "err", "C3 sim → 红（真机掉线才是故障）");
eq(robotPill(links({ robot: { level: "err", state: "offline", label: "未连接", detail: "d" } })).tone,
  "err", "C4 offline → 红");
const rp = robotPill(links({ robot: { level: "warn", state: "sim_forced", label: "强制模拟", detail: "x", host: "192.168.1.12", port: 502, simulate: "always" } }));
ok(rp.title.includes("192.168.1.12:502") && rp.title.includes("simulate=always"),
  "C5 悬浮提示带地址与 simulate 取值（现场排查第一眼要看的东西）");
eq(rp.value, "强制模拟", "C6 value 直接显示后端 label（不在前端另造词）");

// ---------------------------------------------------------------------
section("D. 摄像头灯：五态逐档钉死");
// ---------------------------------------------------------------------
const camCases = [
  ["ready", "ok", "D1 已连接 → 绿"],
  ["opening", "warn", "D2 正在打开 → 黄"],
  ["idle", "warn", "D3 服务通但相机未开 → 黄"],
  ["error", "err", "D4 相机未连接（服务通）→ 红"],
  ["down", "warn", "D5 ★ 服务未启动 → 黄：这是部署状态不是故障，给红会让人去查网线"],
];
for (const [state, tone, msg] of camCases) {
  eq(cameraPill(links({ camera: { level: tone, state, label: state, detail: "d", base: "u" } })).tone, tone, msg);
}
const cp = cameraPill(links({
  camera: { level: "ok", state: "ready", label: "相机已连接", detail: "d", ok: true,
            base: "http://127.0.0.1:8100", device: "MV-CU120-10GM · SN1", ip: "192.168.1.51",
            resolution: "4024x3036", fps: 30 },
}));
ok(cp.detail.includes("MV-CU120-10GM") && cp.detail.includes("192.168.1.51")
  && cp.detail.includes("4024x3036") && cp.detail.includes("30 fps"),
  "D6 连接正常时 detail 展示设备/相机IP/分辨率/帧率");
const cpDown = cameraPill(links({ camera: { level: "warn", state: "down", label: "相机服务未启动", detail: "启动 camera/camera_service.py", base: "u" } }));
ok(cpDown.detail.includes("camera_service.py"), "D7 服务未启动时必须给出「怎么恢复」，而不是只说失败");
eq(cpDown.tone, "warn", "D8 down 仍是黄");

// ---------------------------------------------------------------------
section("E. 示教器档位灯：不管有没有 AUTO 都要显示");
// ---------------------------------------------------------------------
// ★ 2026-09-23 实机确认后语义反转：AUTO/远程 是上位机控制的**唯一有效档位**，
//   T1/T2 下控制器忽略上位机指令。所以这里 AUTO → 绿、T1 → 红（与直觉相反，
//   但这是实测结论，断言必须跟着实测走，不能跟着"官方手册的印象"走）。
const mAuto = modePill(links({ run_mode: {
  level: "ok", mode: "AUTO", label: "AUTO 自动", desc: "d",
  confirmed: true, joggable: true, source: "controller", observed_mode: "AUTO", observed_age_sec: 2,
} }));
eq(mAuto.tone, "ok", "E1 控制器实测 AUTO → 绿（上位机控制的唯一有效档位）");
eq(mAuto.value, "AUTO 自动", "E2 显示具体档位");
eq(mAuto.joggable, true, "E3 joggable=true");
eq(mAuto.source, "controller", "E3b 来源标记为控制器实测（不是人工声明）");
eq(mAuto.sourceText, "控制器实测", "E3c 来源有中文文案可展示");
ok(mAuto.title.includes("控制器实测"), "E3d 悬浮提示写明来源（现场要能分清硬件读到 vs 人说的）");
eq(mAuto.observedAgeSec, 2, "E3e 实测时间可展示（判断这条读数新不新）");

const mUnclaimed = modePill(links({ run_mode: { level: "warn", mode: "", label: "未确认", desc: "d", confirmed: false, joggable: false } }));
eq(mUnclaimed.tone, "warn", "E4 ★ 未确认 → 黄灯且**照常显示**（不是隐藏，这是硬要求）");
eq(mUnclaimed.value, "未确认", "E5 未确认时文案是「未确认」（如实呈现，不假装知道）");
ok(mUnclaimed.title.includes("声明"), "E6 未确认时提示「按旋钮位置声明」（告诉下一步动作）");
eq(mUnclaimed.source, "", "E6b 未确认时来源为空（不假装是实测也不是声明）");

const mT1 = modePill(links({ run_mode: {
  level: "err", mode: "T1", label: "T1 手动低速", desc: "d",
  confirmed: true, joggable: false, source: "manual", actor: "admin",
} }));
eq(mT1.tone, "err", "E7 手动档 T1 → 红（该档控制器忽略上位机指令）");
eq(mT1.sourceText, "手动声明", "E7b 来源为手动声明");
eq(mT1.actor, "admin", "E7c 手动声明带声明人（实测来源没有声明人）");

const mManualGear = modePill(links({ run_mode: {
  level: "err", mode: "T1", label: "手动档（T1/T2）", desc: "d",
  confirmed: true, joggable: false, source: "controller", observed_raw: "manual",
} }));
eq(mManualGear.value, "手动档（T1/T2）", "E7d ★ 控制器只报 manual 时如实写「T1/T2 分不出」，不假装是 T1");
eq(mManualGear.tone, "err", "E7e 手动档 → 红");

const mStale = modePill(links({ run_mode: { level: "warn", mode: "", claimed_mode: "T1", label: "未确认", desc: "d", confirmed: false, stale: true, joggable: false } }));
eq(mStale.tone, "warn", "E8 声明已过期 → 黄（视同未确认）");
eq(mStale.claimedMode, "T1", "E9 过期时仍能看到上次声明的档位（便于用户确认旋钮还在不在原处）");
eq(mUnclaimed.options.map((o) => o.id), RUN_MODES.map((o) => o.id), "E10 未声明时也要带出四档选项（否则没法声明）");

// ---------------------------------------------------------------------
section("F. 缺数据不崩、不误报（这是最容易埋雷的地方）");
// ---------------------------------------------------------------------
for (const bad of [null, undefined, {}, { robot: null }, { robot: {} }, { camera: {} }, { run_mode: {} }]) {
  const pills = allPills(bad);
  ok(pills.length === 3 && pills.every((p) => p.key && p.label && p.tone && p.title),
    "F1 links=" + JSON.stringify(bad) + " 仍产出 3 盏完整灯（不抛异常、不返回空）");
}
eq(robotPill(null).tone, "off", "F2 缺 robot → 灰灯而非绿灯（不假装正常）");
eq(cameraPill(null).tone, "off", "F3 缺 camera → 灰灯");
eq(modePill(null).tone, "off", "F4 缺 run_mode → 灰灯");
ok(allPills(null).every((p) => p.available === false), "F5 缺数据时 available=false（面板可据此隐藏按钮）");
ok(allPills(links()).every((p) => p.available === true), "F6 数据齐全时 available=true");

// ---------------------------------------------------------------------
section("G. 整体色调 / 谁最该被看（底栏一句话摘要的依据）");
// ---------------------------------------------------------------------
eq(stripTone(allPills(links())), "ok", "G1 三绿 → ok");
eq(stripTone(allPills(links({ camera: { level: "err", state: "error", label: "x", detail: "d" } }))), "err",
  "G2 一红 → err（取最差）");
eq(stripTone(allPills(links({ run_mode: { level: "warn", mode: "", label: "未确认", desc: "d", confirmed: false } }))), "warn",
  "G3 一黄 → warn");
eq(stripTone([]), "off", "G4 空集合 → off");
eq(stripTone(null), "off", "G5 null → off");
const mix = allPills(links({
  robot: { level: "warn", state: "sim_forced", label: "强制模拟", detail: "d" },
  camera: { level: "err", state: "error", label: "相机未连接", detail: "d" },
  run_mode: { level: "warn", mode: "", label: "未确认", desc: "d", confirmed: false },
}));
eq(mix.map((p) => p.tone), ["warn", "err", "warn"], "G6 三灯各自独立取色（互不覆盖）");
eq(worstPill(mix).key, "camera", "G7 worstPill 取最差的那一盏");
eq(worstPill(allPills(links())), null, "G8 全 ok → worstPill 返回 null（没有需要提醒的）");

// ---------------------------------------------------------------------
section("H. 声明权限门（没令牌只能看，不能声明）");
// ---------------------------------------------------------------------
const pill = modePill(links());
ok(canClaim(true, pill) === true, "H1 有令牌 + 档位灯 → 可声明");
ok(canClaim(false, pill) === false, "H2 无令牌 → 不可声明");
ok(canClaim(true, robotPill(links())) === false, "H3 机器人灯不是声明入口");

// ---------------------------------------------------------------------
section("I. 与后端字段名对齐（改后端字段名必须同步改这里）");
// ---------------------------------------------------------------------
const sysPy = fs.readFileSync(path.join(REPO, "backend", "app", "api", "system.py"), "utf8");
for (const f of ["state", "level", "label", "detail"]) {
  ok(sysPy.includes(`"${f}":`), `I1 后端 _links 仍在输出 ${f} 字段`);
}
ok(/"_robot_link"|def _robot_link/.test(sysPy), "I2 _robot_link 仍在");
ok(/def _camera_link/.test(sysPy), "I3 _camera_link 仍在");
ok(sysPy.includes('"run_mode": rm'), "I4 links 里的键名仍是 run_mode（与前端 modePill 一致）");
for (const st of ["sim_forced", "offline"]) {
  ok(sysPy.includes('"' + st + '"'), `I5 后端仍产出 ${st} 状态（前端已按它取色）`);
}
ok(sysPy.includes("rm[\"source\"]") || sysPy.includes("rm['source']"),
  "I6 后端档位检查文案按 source 区分实测/声明（否则界面分不清档位是谁给的）");

// ---------------------------------------------------------------------
section("I'. 档位自动确认链路完整（少了任何一环都只会静默退回「未确认」）");
// ---------------------------------------------------------------------
// ★ 这是本类改动最容易"看着没问题"的地方：控制器实测档位靠一条
//   modbus → collector → runmode 的喂数链路。任何一环被删掉，界面不会报错，
//   只会永远显示"未确认" —— 用户看到的现象和功能没做出来一模一样。
//   所以把链路的三段在**源码结构**上钉死。
const runmodePy = fs.readFileSync(path.join(REPO, "backend", "app", "services", "runmode.py"), "utf8");
ok(/def observe\(self/.test(runmodePy), "I'1 runmode 提供 observe()（接收控制器实测档位）");
ok(/OBSERVE_TTL_SEC/.test(runmodePy), "I'2 实测值有独立有效期（控制器掉线后灯要自己回未确认）");
ok(/"source": source/.test(runmodePy), "I'3 state 暴露 source（controller / manual / 空）");
ok(/controller/.test(runmodePy) && /manual/.test(runmodePy), "I'4 两个来源都在 state 里可区分");

const collectorPy = fs.readFileSync(path.join(REPO, "backend", "app", "services", "collector.py"), "utf8");
ok(/def feed_run_mode/.test(collectorPy), "I'5 collector 提供 feed_run_mode（唯一的喂数入口）");
ok(/feed_run_mode\(snap\.get\("mode"\)/.test(collectorPy),
  "I'6 采集循环把快照里的 mode 真的喂了进去（删掉这行不会报错，只会永远未确认）");
ok(/runmode\.observe\(/.test(collectorPy), "I'7 feed_run_mode 真的调到了 runmode.observe");

const robotPy = fs.readFileSync(path.join(REPO, "backend", "app", "api", "robot.py"), "utf8");
ok(/feed_run_mode/.test(robotPy), "I'8 /rc-status 也喂一次（用户手动刷新真机链路时灯要立刻更新）");

// （原 J 段"底栏三盏灯的浮层唯一性"随 StatusStrip.vue 一起删除 —— 单页化后底栏不存在了，
//   链路状态改由大屏底部 KPI 条承载。这段守卫盯的是组件模板结构，组件没了它就没有对象。

// ---------------------------------------------------------------------
section("K. 连接反馈文案：成功只说结论");
// ---------------------------------------------------------------------
// 现场要求："连接成功后显示连接成功就行了"。成功路径不许再堆链路细节 ——
// 链路细节由浮层顶部的状态行承担，反馈行只回答"通没通"。
const linkSrc = fs.readFileSync(path.join(ROOT, "src", "stores", "link.js"), "utf8");
ok(linkSrc.includes('d.connected ? "连接成功"'), "K1 机器人重连成功 → 「连接成功」");
ok(!linkSrc.includes("已切回真实链路"), "K2 旧的冗长成功文案已移除");
ok(/d\.connected \? "连接成功" : "[^"]*失败/.test(linkSrc), "K3 失败路径仍保留原因（不能只报「失败」）");
ok(linkSrc.includes("需要控制权限才能重连"), "K4 401/403 仍单独提示需要权限（不然用户不知道去点登录）");

// ---------------------------------------------------------------------
section("L. 项目名三处一致（index.html / brand.js / 后端 brand.py）");
// ---------------------------------------------------------------------
// 项目名散落在三个地方：静态 <title>（hydration 前显示）、前端 BRAND.name（顶栏与
// document.title）、后端 SERVICE_NAME（/api/version、OpenAPI 标题、导出包头部）。
// 只改其中一两处 → 浏览器标签页一个名字、切到 /docs 又是另一个名字，
// 现场会以为连了两套系统。改名是高频操作，必须在测试里钉住。
const { BRAND } = await import(pathToFileURL(path.join(ROOT, "src", "brand.js")).href);
const htmlSrc = fs.readFileSync(path.join(ROOT, "index.html"), "utf8");
const titleM = htmlSrc.match(/<title>([^<]*)<\/title>/);
ok(!!titleM, "L1 index.html 里有 <title>");
ok(!!titleM && titleM[1].trim().startsWith(BRAND.name),
  `L2 静态标题以 BRAND.name 开头（标题=${titleM ? titleM[1].trim() : "?"}）`);
const brandPy = fs.readFileSync(path.join(REPO, "backend", "app", "core", "brand.py"), "utf8");
const nameM = brandPy.match(/^SERVICE_NAME\s*=\s*"([^"]*)"/m);
ok(!!nameM, "L3 能在 brand.py 里定位 SERVICE_NAME");
eq(nameM ? nameM[1] : null, BRAND.name, "L4 ★ 前后端项目名完全一致");
ok(!/FIT 机器人远程监控与操控平台/.test(htmlSrc + brandPy),
  "L5 已弃用的旧名不再出现在静态标题与后端");
// ★ 2026-10-06 定名调整：产品从"管一台埃夫特"变成"一块屏看一整个机房（埃夫特 + 华沿）"，
//   所以全名里**不能**再出现单一厂商名 —— 挂"埃夫特"会让华沿那 6 台在标题上不存在。
//   厂商/型号口径挪到 BRAND.device（顶栏副标题），这里钉住它仍然说清两个牌子。
ok(!/埃夫特|EFORT|华沿|ELFIN/.test(BRAND.name),
  "L6 全名不含单一厂商名（机房里两个牌子，标题只写'机房'）");
ok(/机房/.test(BRAND.name), "L6a 全名点明'机房'口径（一块屏管一屋子，不是单台）");
ok(/埃夫特/.test(BRAND.device) && /华沿/.test(BRAND.device),
  "L6d 厂商混编仍在副标题 device 里可见（顶栏要能看出管的是哪两种机器）");
// ★ 阶段 7 定名调整：应用户要求全名**去掉 AI**（「FIT 埃夫特智能机器人远程控制与监控系统」）。
//   钉两件事：中英文名都不再含 AI（改名要两边同步，防止单边残留），且禁用名不回流。
ok(!/AI/.test(BRAND.name), "L6b 中文全名不含 AI（应用户要求的定名）");
ok(!/\bAI\b/.test(BRAND.en), "L6c 英文全名不含 AI");
ok(BRAND.abbr === BRAND.exportPrefix, "L7 缩写与导出前缀同源（避免导出文件名用另一套缩写）");
// 缩写刻意不含 AI：它是导出文件名前缀，现场已有带该前缀的导出文件，改了会让新旧对不上。
eq(BRAND.abbr, "FIT-RCMS", "L8 缩写保持 FIT-RCMS 不变（导出文件名兼容性）");

console.log(`\n${fail === 0 ? "全部通过" : "存在失败"}：${pass} passed, ${fail} failed`);
process.exit(fail === 0 ? 0 : 1);
