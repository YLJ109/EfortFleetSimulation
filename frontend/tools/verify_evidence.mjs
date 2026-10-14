// =====================================================================
// A5「事件取证包」前端接线守卫（headless 静态检查）
//
// 取证包是"每一步都能自证"的**落地物**，界面这一侧最容易犯的错有四类，
// 每一类都在现场真出过问题，所以逐条钉死：
//   ① **水位用数组长度** —— 列表会被 keep_days / max_packs 裁短，长度不单调，
//      用 `packs.length` 做刷新判据就会"悄悄不再更新"（§5.5 铁律①，log.length 同一个坑）；
//   ② **未知塌成布尔** —— `interlocked ? "开" : "关"` 会把 null（没读到）说成"关"（铁律③）；
//   ③ **替后端圆场** —— honest_gaps（"这次没采到什么"）不显示，复盘时就以为"当时没拍"；
//   ④ **假入口** —— 报警横幅挂一个点了跳不过去的"查看取证"（运维审计页是 needAdmin）。
//
// 运行： cd frontend && node tools/verify_evidence.mjs
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

const ev = read("src/components/EventsView.vue");
const app = read("src/App.vue");
const html = read("index.html");
const api = read("../backend/app/api/safety.py");
const svc = read("../backend/app/services/safety_evidence.py");

// 只截取证包那一段（脚本里还有别的功能，不能拿整文件判"有没有长度水位"）
const secStart = ev.indexOf("// ---------------- 事件取证包");
const secEnd = ev.indexOf("// ---------------- WebSocket 实时追加");
const panel = secStart >= 0 && secEnd > secStart ? ev.slice(secStart, secEnd) : "";

// ---------------------------------------------------------------------
console.log("");
console.log("== A. 接口口径：三个只读接口 + 全部带控制令牌 ==");
{
  ok("A1 列表走 apiControl(/safety/evidence?limit=30)",
     /apiControl\("\/safety\/evidence\?limit=30"\)/.test(panel));
  ok("A2 详情走 apiControl(/safety/evidence/{id})",
     /apiControl\(`\/safety\/evidence\/\$\{encodeURIComponent\(p\.id\)\}`\)/.test(panel));
  ok("A3 整包下载走 /zip 且转 blob 落地（window.open 带不了令牌）",
     /apiControl\(`\/safety\/evidence\/\$\{encodeURIComponent\(p\.id\)\}\/zip`\)/.test(panel)
     && /await r\.blob\(\)/.test(panel) && /a\.download = `\$\{p\.id\}\.zip`/.test(panel));
  ok("A4 包 id 一律 encodeURIComponent（后端另有防目录穿越，前端不递裸串）",
     (panel.match(/encodeURIComponent\(p\.id\)/g) || []).length >= 2);
  ok("A5 未持控制令牌时**不发请求**（避免开机每个访客刷一条 401）",
     /if \(!auth\.controlActive\) \{ packs\.value = \[\]; packTotal\.value = 0; return; \}/.test(panel));
  ok("A6 401/403 只清空列表并请求登录，不谎报「没有取证包」",
     /if \(r\.status === 401 \|\| r\.status === 403\) return auth\.requestLogin\(\);/.test(panel));
  ok("A7 界面这一侧**只读**：取证段里没有任何 POST/PUT/DELETE",
     !/method:\s*"(POST|PUT|DELETE|PATCH)"/.test(panel));
  ok("A8 后端三个接口都挂 require_control（前端读到才说明有权限）",
     (api.match(/evidence[\s\S]{0,260}?Depends\(require_control\)/g) || []).length >= 3);
}

// ---------------------------------------------------------------------
console.log("");
console.log("== B. ★ 刷新水位用 latest_seq，不用数组长度（§5.5 铁律①） ==");
{
  ok("B1 列表响应里取 latest_seq 存进 packLatestSeq",
     /packLatestSeq\.value = d\.latest_seq/.test(panel));
  ok("B2 只在变大时才推进（单调，不因裁短而回落）",
     /if \(\(d\.latest_seq \|\| 0\) > packLatestSeq\.value\)/.test(panel));
  ok("B3 界面显示的计数是 #{{ packLatestSeq }}（不是 packs.length）",
     /最新序号 #\{\{ packLatestSeq \}\}/.test(ev));
  ok("B4 取证段里**没有**用 packs.length 做水位/判据",
     !/packs\.value\.length\s*[<>!=]/.test(panel));
  ok("B5 后端 list 确实带 latest_seq（与 total 无关，来自 seq 最大者）",
     /"latest_seq":\s*latest/.test(svc) && /rows\.sort\(key=lambda r: int\(r\.get\("seq"\)/.test(svc));
  ok("B6 seq 落盘后**不复用**（删包也不回退）",
     /def _next_seq/.test(svc) && /while os\.path\.exists\(final\)/.test(svc));
  ok("B7 后端刚落盘 → WS 事件立刻刷新列表（不靠轮询撞运气）",
     /m\.action === "safety\.evidence_written"/.test(ev));
  ok("B8 轮询兜底仍在（WS 断了列表也会自己更新，取证列表 10s 一次）",
     /if \(\+\+tickN % 2 === 0\) loadPacks\(\);/.test(ev));
}

// ---------------------------------------------------------------------
console.log("");
console.log("== C. 三态与缺失：不许塌成布尔、不许静默省略 ==");
{
  ok("C1 tri() 把 null/undefined 显式说成「未知」",
     /if \(v === null \|\| v === undefined\) return "未知";/.test(panel));
  ok("C2 num() 把 null/undefined 显示成 —（不塌成 0）",
     /return \(v === null \|\| v === undefined\) \? "—" : `\$\{v\}\$\{unit\}`/.test(panel));
  ok("C3 互锁/实测/标定/陈旧帧都走 tri()（不是 x ? 是 : 否 那种写法）",
     /tri\(f\.interlocked/.test(panel) && /tri\(srv\.measured/.test(panel)
     && /tri\(srv\.calibrated/.test(panel) && /tri\(pf\.stale/.test(panel));
  ok("C4 伺服/急停/报警位走 tri()（寄存器没读到 = 未知，不是说「没上电」）",
     /tri\(rc\.servo_on/.test(panel) && /tri\(rc\.estop_bit/.test(panel)
     && /tri\(rc\.alarm_bit/.test(panel));
  ok("C5 间隙 mm 为 null 时显示 —（不是 0mm 这种看着像事实的读数）",
     /间隙 \{\{ p\.clearance_mm \?\? "—" \}\}mm/.test(ev));
  ok("C6 新鲜度三态原样呈现（never / fresh / stale）",
     /srv\.freshness \|\| "never"/.test(panel) && /cli\.freshness \|\| "never"/.test(panel));
  ok("C7 陈旧帧写明「关节不代表控制器当前位置」（不冒充实时位姿）",
     /关节不代表控制器当前位置/.test(panel));
  ok("C8 honest_gaps 逐条渲染（缺几条列几条，不折叠成一句「有缺失」）",
     /<li v-for="\(g, gi\) in p\.honest_gaps" :key="gi">\{\{ g \}\}<\/li>/.test(ev));
  ok("C9 行首给「缺 N」角标，一眼看出这条取证有洞",
     /class="opk-gap">[\s\S]{0,60}缺 \{\{ p\.honest_gaps\.length \}\}/.test(ev));
  ok("C10 卡片说明写清「缺什么在包里逐条写明」（口径进界面，不只进注释）",
     /缺什么在包里逐条写明/.test(ev));
  ok("C11 速度口径**只读**并注明「取证不改速度」（A3 的红线不因取证破例）",
     /只读，取证不改速度/.test(panel));
  ok("C12 免责声明进界面（不是安全认证材料 / 不替代控制器安全板）",
     /不替代控制器安全板/.test(ev) && /disclaimer/.test(panel));
  ok("C13 没触发过就如实说「没有」（列表空 ≠ 报错，也不编一条演示数据）",
     /没触发过 danger\/hit 就是没有，不会凭空造/.test(ev));
}

// ---------------------------------------------------------------------
console.log("");
console.log("== D. 报警横幅的「查看取证」入口：能点到才算存在 ==");
{
  ok("D1 横幅上有「查看取证」按钮", /class="sa-ev"[\s\S]{0,160}查看取证/.test(app));
  ok("D2 点击 = setView('ops')（真的跳到运维审计）",
     /class="sa-ev"[\s\S]{0,200}@click="setView\('ops'\)"/.test(app));
  ok("D3 ★ 只有能进 ops 页（needAdmin）才渲染该按钮 —— 不挂假入口",
     /v-if="allowedKeys\.includes\('ops'\)"/.test(app));
  ok("D4 ops 确实是需要管理员令牌（钉住 D3 的前提）",
     /key: "ops"[\s\S]{0,80}needAdmin: true/.test(read("src/tabs.js")));
  ok("D5 按钮样式跟着报警配色走（currentColor 描边，不写死颜色）",
     /#safety-banner \.sa-ev \{[\s\S]{0,200}color: inherit;[\s\S]{0,80}border: 1px solid currentColor/.test(html));
}

// ---------------------------------------------------------------------
console.log("");
console.log("== E. 取证包本身的行为钉（与后端一致，防止界面先退） ==");
{
  ok("E1 列表按 seq 倒序（最新在前）",
     /reverse=True/.test(svc.split("def list_packs")[1].split("def _seq_of")[0]));
  ok("E2 半成品（.pending-*）不进列表 —— 界面不会看到「缺 manifest 的假损坏包」",
     /not n\.startswith\("\."\)/.test(svc));
  ok("E3 manifest 读不出来时如实标 corrupt + 一条 gap（不静默跳过）",
     /"corrupt": True/.test(svc) && /manifest\.json 缺失或损坏/.test(svc));
  ok("E4 详情接口返回 manifest + files（界面展开用的就是这两块）",
     /"files"/.test(svc.split("def get_pack")[1].split("def _tail_lines")[0])
     && /packDetail\.files/.test(ev) && /packDump\(packDetail\)/.test(ev));
  ok("E5 取证不落「看着像事实」的 NaN/Inf（非有限浮点转 null）",
     /float\("inf"\)/.test(svc) && /math\.isfinite|!= |isfinite/.test(svc));
  ok("E6 采集循环里只排队不落盘（界面拿到的包不会拖慢采集）",
     /只排队、不落盘/.test(read("../backend/app/services/safety_guard.py")));
}

console.log("");
console.log("结果: " + pass + " 通过 / " + fail + " 失败");
console.log("");
process.exit(fail ? 1 : 0);
