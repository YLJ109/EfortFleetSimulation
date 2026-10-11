// =====================================================================
// 真机链路就绪卡回归（headless，源码扫描）
//
// 钉死后续最容易回归的几点：
//  A. store 通道唯一：/rc-status 只走 apiUrl（公开只读，不带令牌）；
//     一键就绪只走 apiControl("/ready")（写寄存器必须带控制令牌）。
//     ★ 坑27 守卫：apiUrl/apiControl 会自动补 /api 前缀，路径绝不能再带 /api。
//  B. 档位声明不另开通道：claimMode 必须委托 link.claimMode()。
//  C. 卡片挂载：就绪卡统一在底部「真机链路」浮层（LinkPanel 内嵌 RcReadyCard，App 挂 LinkPanel），
//     点位页不再各自挂载；程序执行页改用就绪盒读 rc.ready；状态行七项齐全；
//     就绪按钮为"绿(一键就绪)/红(取消就绪)"单按钮；禁用门控在 readyDisabled 计算属性里，
//     必须覆盖「无令牌或机器人未连接（canControl）/ 双闸未开(realEnabled) / 触发位占用(jogTrig) / 执行中(busy)」。
//  D. 图标名必须真实存在于 Icon.vue（动态 name 拼错只会静默空白）。
//  E. 后端契约：/rc-status 返回 ready/real_enabled/service_program；
//     /ready 挂 require_control。
//
// 运行： node tools/verify_rc_ready.mjs
// =====================================================================
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (p) => readFileSync(join(root, p), "utf8");

let pass = 0, fail = 0;
function ok(name, cond, extra = "") {
  if (cond) { pass++; console.log("  PASS  " + name); }
  else { fail++; console.log("  FAIL  " + name + (extra ? "   → " + extra : "")); }
}

console.log("== 真机链路就绪卡回归 ==");

const store = read("src/stores/rcReady.js");
const card = read("src/components/RcReadyCard.vue");
const pointView = read("src/components/PointExecView.vue");
const progView = read("src/components/ProgramExecView.vue");
const linkPanel = read("src/components/LinkPanel.vue");
const statusStrip = read("src/components/StatusStrip.vue");
const app = read("src/App.vue");
const icons = read("src/components/Icon.vue");
const robotApi = read("../backend/app/api/robot.py");
const rcReady = read("../backend/app/services/rc_ready.py");
const robotStore = read("src/stores/robot.js");
const wsNet = read("src/net/ws.js");
const readySvc = read("../backend/app/services/rc_ready.py");

// ---------- A. store 通道唯一 + 路径不带 /api 前缀 ----------
ok("rc-status 走 apiUrl（公开只读，无需令牌）",
  store.includes("fetch(apiUrl(\"/rc-status\"))"));
ok("一键就绪走 apiControl /ready（写寄存器须带控制令牌）",
  store.includes("apiControl(\"/ready\""));
ok("apiUrl 路径不带 /api 前缀（坑27：会变成 /api/api/...）",
  !/apiUrl\(\s*["'`]\/api\//.test(store));
ok("apiControl 路径不带 /api 前缀",
  !/apiControl\(\s*["'`]\/api\//.test(store));
ok("轮询周期常量 RC_POLL_MS 已定义（现为 WS 新鲜度窗口）", /export const RC_POLL_MS\s*=\s*\d+/.test(store));
ok("数据经 WS 注入而非轮询：net/ws.js 分发 rc_status → robot store → rcReady.ingest",
  wsNet.includes('msg.type === "rc_status"')
  && robotStore.includes("useRcReadyStore().ingest(rc)")
  && store.includes("ingest(payload)"));
ok("rcReady 不再起定时轮询（无 setInterval 残留）", !/setInterval/.test(store));

// ---------- B. 档位声明唯一通道 ----------
ok("claimMode 委托 link.claimMode（档位声明绝不另写一份）",
  /const link = useLinkStore\(\);/.test(store)
  && /link\.claimMode\(mode\)/.test(store));

// ---------- C. 卡片挂载与门控 ----------
// ★ 架构变更（2026-10-10）：就绪卡不再由各页各自挂载。原先只有「点位执行」「分拣」两页的
//   可折叠右栏里有它 → 现场反映"别的页面也没有一键就绪"。统一收进底部「真机链路」入口：
//   App 提供切换按钮 + #bottom-zone —— 任何页面都能点。
// ★★ 架构变更（2026-10-11）：底部状态区改为**五选一**（StatusStrip：运行日志/机器人/
//   摄像头/示教器/真机链路）—— 「真机链路」是其中一片，面板内容仍是 RcReadyCard。
//   原来独立的切换按钮 + LinkPanel 浮窗已并入这一片。
ok("就绪卡由底部状态区「真机链路」片统一挂载（StatusStrip 内嵌 RcReadyCard）",
  statusStrip.includes("import RcReadyCard from") && statusStrip.includes("<RcReadyCard"));
ok("App 底栏挂 StatusStrip（五选一，含真机链路片）+ bottom-zone",
  app.includes("<StatusStrip") && app.includes("bottom-zone"));
ok("LinkPanel 仍内嵌 RcReadyCard（保留文件，仅入口已并入五选一）",
  linkPanel.includes("import RcReadyCard from") && linkPanel.includes("<RcReadyCard />"));
ok("点位执行页不再各自挂载 RcReadyCard（已收进底栏浮层，避免出现第二份）",
  !pointView.includes("<RcReadyCard />") && !pointView.includes("import RcReadyCard from"));
// ★ 程序执行页已按现场要求把「真机链路卡 + 控制权限卡」合成一个就绪提示盒：
//   不再挂 RcReadyCard，只读 useRcReadyStore().ready 给出一句结论（见 ProgramExecView）。
ok("程序执行页不再挂 RcReadyCard（改用就绪提示盒）", !progView.includes("<RcReadyCard />"));
ok("程序执行页不再 import RcReadyCard 组件", !progView.includes("import RcReadyCard from"));
ok("程序执行页就绪盒仍读 rc.ready 合成结论",
  progView.includes("useRcReadyStore") && /rc\.ready\s*&&/.test(progView));

for (const k of ["双闸", "档位", "伺服", "报警", "急停", "程序", "点动"]) {
  ok(`状态行「${k}」存在`, card.includes(`k: "${k}"`));
}
ok("有一键就绪按钮", card.includes("一键就绪"));
// ★ 结构升级（用户要求）：6 按钮合并为"绿/红就绪切换"单按钮 + 移除「刷新/声明 AUTO/声明远程」。
//   禁用门控从内联 :disabled 收进 readyDisabled 计算属性 —— 断言随之改为核对该计算属性。
ok("就绪中反馈文案", card.includes("处理中"));
ok("一键就绪在无控制令牌 / 机器人未连接时禁用（判据只此一处：canControl）",
  /const canControl = computed\(\(\) => auth\.controlActive && robot\.connected/.test(card)
  && /if \(!canControl\.value\) return true;/.test(card));
ok("一键就绪在双闸未开时禁用", /if \(!rc\.realEnabled\) return true;/.test(card));
// ★ 现场事故：触发位被留在 1 时原实现直接**禁用**按钮 → 用户再也点不了"一键就绪"（死锁）。
//   正解是按现场铁律"加载前只需清报警 + 撤触发"—— 后端就绪流程第一步撤触发，前端只提示不拦。
ok("不再因触发位为 1 而禁用一键就绪（改为提示，由后端先撤触发）",
  !/return rc\.jogTrig;/.test(card) && card.includes('v-if="rc.jogTrig"'));
ok("后端就绪流程包含「撤触发位」步骤（写 40135 = 0）",
  /ADDR_RO_TRIG,\s*0x0000/.test(readySvc) && readySvc.includes("trig_clear"));
ok("就绪执行中禁用重复触发",
  /rc\.busy/.test(card) && card.includes(':disabled="readyDisabled"'));
// ★ 真实事故修复：取消就绪曾与"发起就绪"共用整套前置判据（含机器人连接/遥测新鲜），
//   伺服一停按钮就整块变灰 —— 既不能取消也不能再次就绪。它是安全动作，必须永远可点。
ok("取消就绪永远可点（只要求持有控制令牌）",
  /if \(readyAction\.value === "cancel"\) return !auth\.controlActive;/.test(card));
ok("取消就绪会一并伺服下电（cancelReady(true)）",
  card.includes("rc.cancelReady(true)"));

// ★★ 2026-10-08 语义统一回归（用户："取消就绪伺服还是上电的状态"）：
//   「取消就绪」曾有两个入口而行为不同 —— 就绪卡下电、底栏只停机 → 同名不同义，
//   用户按下底栏那条后看到"伺服还是上电"。现统一为"停机 + 下电"，两条路径都必须下电，
//   且不许再出现 cancelReady(false)（那会把伺服留在上电态）。
// ★ 2026-10-10：底栏那条「取消就绪/下电」按钮已收进就绪卡（bottom-zone 只留真机链路切换），
//   于是**就绪卡成为唯一的取消/下电入口**。断言随之改为核对卡片内的下电与安全确认。
ok("唯一的取消/下电入口就在就绪卡（底栏不再另放一条，避免同名不同义）",
  card.includes("rc.cancelReady(true)") && !/cancelReady/.test(app));
ok("全局不再存在 cancelReady(false)（避免同名不同义回归）",
  !/cancelReady\(\s*false\s*\)/.test(app) && !/cancelReady\(\s*false\s*\)/.test(card));
// ★★ 2026-10-10 用户口径：**一键就绪 / 取消就绪 合并为一颗按钮**，点击时先读实时伺服态。
//   下电**不再弹确认框**（用户："不需要提示"）—— 断言随之从"必须有 confirm"反转为"必须没有"。
ok("★★ 就绪/取消已合并为**单颗**按钮（只有一颗 ready-toggle + onReadyToggle）",
  (card.match(/class="ready-toggle"/g) || []).length === 1
  && /onReadyToggle/.test(card));
ok("★★ 点击时**先读一次实时伺服状态**再决定（rc.load() → 看 servoOn）",
  /async function onReadyToggle[\s\S]{0,400}?await rc\.load\(\)/.test(card)
  && /async function onReadyToggle[\s\S]{0,700}?rc\.servoOn/.test(card));
ok("★★ 下电**不再弹确认框**（2026-10-10 用户口径：不需要提示）",
  !/window\.confirm/.test(card));
// ── 清除报错（清控制器报警）：唯二入口（就绪卡 + 底栏真机链路片复用同一卡） + 只清报警不替就绪做上电/加载 ──
const sortView = read("src/components/SortingView.vue");
const execStore = read("src/stores/exec.js");
const controlApi = read("../backend/app/api/control.py");
const readySvcBackend = read("../backend/app/services/rc_ready.py");
ok("exec store 有 clearError（清示教器报错）", /clearError\s*\(/.test(execStore));
ok("exec.clearError 打 /control/clear-error", /\/control\/clear-error/.test(execStore));
ok("点位/分拣页共用的就绪卡有「清除报错」按钮", card.includes("清除报错") && card.includes("exec.clearError()"));
// ★★ 2026-10-11 修复（方案 Pr3）：程序执行页原另有一颗「清除报错」，与底栏「真机链路」面板里的
//   RcReadyCard **同名同义重复**（底栏任何页都能点开）。现统一走底栏那一个入口，本页不再重复。
ok("程序执行页不再重复「清除报错」（统一走底栏「真机链路」面板）",
  !progView.includes("清除报错") && !progView.includes("exec.clearError()"));
ok("后端 /control/clear-error 挂 require_control",
  /def api_clear_error\(tok: str = Depends\(require_control\)\)/.test(controlApi));
ok("后端 clear_alarm 只清报警（不写 LOAD/RUN，不替就绪做上电加载）",
  /def _clear_alarm[\s\S]*?return/.test(readySvcBackend) &&
  !/def _clear_alarm[\s\S]{0,3000}CMD_LOAD/.test(readySvcBackend));
// ── 一键就绪后自动刷新点位 ──
ok("store 就绪成功递增 readyTick（供各页自动刷新点位）",
  /readyTick/.test(store) && /if \(d\.ok\) this\.readyTick \+= 1;/.test(store));
ok("就绪卡 watch readyTick → 刷新点位", /watch\(\(\) => rc\.readyTick/.test(card) && card.includes("exec.loadPoints()"));
ok("分拣页 watch readyTick → 刷新点位与 38 点体检",
  /watch\(\(\) => rc\.readyTick/.test(sortView) && sortView.includes("loadCheck()"));
ok("程序执行页 watch readyTick → 刷新点位/程序",
  /watch\(\(\) => rc\.readyTick/.test(progView) && progView.includes("exec.loadPoints()"));
// ── 伺服下电命令序列：必须 0x2000(Bit13 取消使能) → 0x0000，并回读校验 ──
// ★ 2026-10-09：窗口从 400 放宽到 2000 —— 两步之间**本来就该有等待与注释**
//   （"等状态位落下再全清"，见 backend/tests/test_ready_servo_timing.py 的 7 条时序回归）。
//   这里只该验证**先后顺序**，不该限制两步之间能写多少行代码。
//   ⚠ 更强的判据在 pytest 那边（"状态位落下前不得出现 0x0000"），这里只做静态兜底。
ok("后端伺服下电顺序正确：先 0x2000 取消使能 → 再 0x0000 全清",
  (() => {
    const m = /if servo_off:[\s\S]{0,1200}?CMD_DISABLE[\s\S]{0,2000}?CMD_ZERO/.exec(readySvcBackend);
    return !!m;
  })());
ok("后端伺服下电有回读校验（状态位没落必须报失败）",
  /servo_down/.test(readySvcBackend) && /伺服状态位/.test(readySvcBackend));
ok("modbus 定义了 CMD_DISABLE = 0x2000", /CMD_DISABLE\s*=\s*0x2000/.test(read("../backend/app/services/modbus.py")));
// ── 分拣：只在真到 C 区才判色（空跑/双闸未开不许因"检测不到"停） ──
const sortApi = read("../backend/app/api/sorting.py");
ok("分拣判色有前置条件（judge = 非空跑 且 双闸已开）",
  /judge = \(not dry\) and real/.test(sortApi));
ok("分拣空跑不判色、不因检测不到停（跳过 _capture）",
  /if not judge:/.test(sortApi) && sortApi.includes("空跑·未判色"));
ok("分拣真实下发前校验确实到达 C 检测点",
  /_camera_deviation/.test(sortApi) && sortApi.includes("CAM_ARRIVE_TOL_DEG"));
ok("运行日志已并入底部状态区（StatusStrip 的「运行日志」片，各页共用）",
  read("src/components/StatusStrip.vue").includes("运行日志"));
ok("分拣页写运行日志（exec.logLine）", /\[分拣\]/.test(sortView) && /exec\.logLine\(/.test(sortView));

// 卡片语义：实测档位反转 —— AUTO/远程可动，T1/T2 拒绝
ok("store 语义反转：仅 auto/remote 可点动",
  /m === "auto" \|\| m === "remote"/.test(store));
ok("手动档文案标注 T1/T2 指令无效", card.includes("手动（T1/T2）") || store.includes("手动（T1/T2）"));

// ---------- D. 图标名真实存在 ----------
const iconNames = [...icons.matchAll(/^\s{2}(\w+):\s*'/gm)].map((m) => m[1]);
const usedIcons = [...card.matchAll(/<Icon[^>]*name="(\w+)"/g)].map((m) => m[1]);
const dynamicIcons = [...card.matchAll(/:name="rc\.lastResult\.ok \? '(\w+)' : '(\w+)'"/g)]
  .flatMap((m) => [m[1], m[2]]);
const all = [...new Set([...usedIcons, ...dynamicIcons])];
for (const n of all) {
  ok(`图标「${n}」存在于 Icon.vue`, iconNames.includes(n));
}

// ---------- E. 后端契约 ----------
ok("后端 /rc-status 返回 real_enabled", robotApi.includes('"real_enabled"'));
ok("后端 /rc-status 返回 service_program", robotApi.includes('"service_program"'));
ok("后端就绪判定 ready（伺服+程序+运行+自动/远程+无报警+非急停）",
  robotApi.includes('snap["ready"]'));
ok("后端 /ready 挂 require_control 门控",
  /def ready\(.*Depends\(require_control\)/s.test(robotApi));

// ---------- F. 按钮该显示「一键就绪」还是「取消就绪」（2026-10-09 修） ----------
// 用户口径：**伺服上电就该显示「取消就绪」；没上电才需要「一键就绪」**。
// 旧判据用 `rc.ready`（= 伺服 AND 程序已加载 AND 运行位 AND AUTO AND 无报警），
// 于是出现现场那个怪现象：伺服明明上着电，但只要程序没加载/没运行，
// `ready=false` → 按钮仍写「一键就绪」→ 操作员不知道该点哪个。
ok("★ 后端额外给出 servo_on（只看伺服位，不受程序/运行位影响）",
  robotApi.includes('snap["servo_on"]')
  && /servo_on.*b\["servo"\]/s.test(robotApi));
ok("★ 前端 store 暴露 servoOn，且**后端没给时返回 null**（不是 false —— 那会让按钮永远显示一键就绪）",
  /servoOn:\s*\(s\)\s*=>\s*\{/.test(store) && /return\s+\(typeof v === "boolean"\)\s*\?\s*v\s*:\s*null/.test(store));
ok("★ 按钮动作跟 servoOn 走（旧后端回落 ready）",
  /const byServo = rc\.servoOn/.test(card)
  && /typeof byServo === "boolean" \? byServo : rc\.ready/.test(card));
ok("「取消就绪」仍然任何情况下都可点（安全动作不能被禁用）",
  /if \(readyAction\.value === "cancel"\) return !auth\.controlActive/.test(card));

// ---------- G. 被点动占用时：主动下电，而不是干等 ----------
// 用户口径：「有点动/下发正在执行，已推迟一键就绪 …… 如果再触发这个条件**直接下电伺服**，
// 等待一键就绪吧」
ok("★ 就绪被占用时走「等收尾 → 取消就绪（含下电）」的补救路径",
  /_defer_then_servo_off/.test(rcReady)
  && /_cancel\(servo_off=True\)/.test(rcReady));
ok("★ 而且必须**带超时地等锁**（不能抢写指令字 —— 那正是要避免的半截状态）",
  /_exec_lock\.acquire\(timeout=/.test(rcReady));
ok("超时未拿到锁时如实回报（不假装已下电）",
  /仍未释放/.test(rcReady));
ok("补救失败/超时的返回里 ok=False（本次确实没完成一键就绪）",
  /def _defer_then_servo_off[\s\S]{0,900}?"ok": False/.test(rcReady));

console.log("");
console.log("结果: " + pass + " 通过 / " + fail + " 失败");
process.exit(fail ? 1 : 0);
