/**
 * headless 回归：机房大屏的卡片口径（src/utils/fleetCards.js）
 *
 * 为什么值得单独立一份断言：大屏上 12 张卡同时显示"这台是什么状态"，而两个牌子的
 * 状态位**字段名根本不同**（埃夫特 alarm/estop/running/servo_on，华沿 error_code/
 * moving/enabled/inpos）。这类映射写错了不会报错，只会安静地把"报警"显示成"就绪"
 * —— 在只读大屏上，那就是唯一的安全出口被堵住。所以把判据钉死在这里。
 *
 * 字段口径来源（改这里之前先回去看）：
 *   backend/app/services/drivers.py:184  埃夫特 rc_snapshot → status
 *   backend/app/services/drivers.py:398  华沿 DataSheet     → status
 *
 * 运行：node tools/verify_fleet_cards.mjs   （已挂在 npm run verify 里）
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..");

// ★ Windows 下动态 import 必须走 file:// URL —— 直接给 "D:\...\x.js" 会被当成
//   scheme "d:" 而报 ERR_UNSUPPORTED_ESM_URL_SCHEME。
const {
  LEVEL_RANK, levelOf, movingOf, statusText, speedOf, autoOf, ageText, fmtNum, fleetBanner,
  makeCard, shortName, faultOf, linkLabel,
} = await import(pathToFileURL(path.join(ROOT, "src", "utils", "fleetCards.js")).href);

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

// ---------------------------------------------------------------------
section("A. levelOf：离线 > 一切；控制器报警 > 围栏碰撞");
// ---------------------------------------------------------------------
eq(levelOf(false, { alarm: true }, "hit"), "offline", "A1 离线就是离线（哪怕快照里还留着报警位）");
eq(levelOf(true, {}, "safe"), "safe", "A2 在线 + 无状态位 + 围栏安全 → safe");
eq(levelOf(true, { servo_on: true }, ""), "safe", "A3 围栏还没算过（safety 为空）→ safe，不是 offline");
eq(levelOf(true, { alarm: true }, "safe"), "alarm", "A4 埃夫特报警 → alarm");
eq(levelOf(true, { estop: true }, "safe"), "alarm", "A5 埃夫特急停 → alarm");
eq(levelOf(true, { error_code: 1023, error_axis: 2 }, "safe"), "alarm", "A6 华沿 error_code 非 0 → alarm");
eq(levelOf(true, { error_code: 0 }, "safe"), "safe", "A7 华沿 error_code 为 0 不算报警");
eq(levelOf(true, { error_code: "0" }, "safe"), "safe", "A8 error_code 是字符串 \"0\" 也不算报警（别被真值判断骗了）");
eq(levelOf(true, {}, "warn"), "warn", "A9 围栏 warn 原样透传");
eq(levelOf(true, {}, "danger"), "danger", "A10 围栏 danger 原样透传");
eq(levelOf(true, {}, "hit"), "hit", "A11 围栏 hit 原样透传");
// ★ 这条是"报警排在碰撞之上"的钉子：现场先要看见"哪台出事了"，而不是"哪台快碰到了"。
eq(levelOf(true, { alarm: true }, "hit"), "alarm", "A12 报警 + 碰撞同时发生 → 报 alarm（更该先看）");
eq(levelOf(true, null, "danger"), "danger", "A13 status 为 null 不炸");
eq(levelOf(true, undefined, undefined), "safe", "A14 全 undefined → safe");
ok(LEVEL_RANK.alarm > LEVEL_RANK.hit && LEVEL_RANK.hit > LEVEL_RANK.danger
   && LEVEL_RANK.danger > LEVEL_RANK.warn && LEVEL_RANK.warn > LEVEL_RANK.safe
   && LEVEL_RANK.offline < LEVEL_RANK.safe, "A15 LEVEL_RANK 单调（fleetBanner 靠它挑最严重的一台）");

// ---------------------------------------------------------------------
section("B. movingOf / speedOf：两个牌子各有一套字段");
// ---------------------------------------------------------------------
eq(movingOf({ running: true }), true, "B1 埃夫特 running → 在动");
eq(movingOf({ moving: true }), true, "B2 华沿 moving → 在动");
eq(movingOf({ running: false, moving: false }), false, "B3 两个都为 false → 没动");
eq(movingOf({}), false, "B4 没字段 → 没动（不能当成在动）");
eq(movingOf(null), false, "B5 null 不炸");
eq(speedOf({ speed_pct: 5 }), 5, "B6 埃夫特 speed_pct");
eq(speedOf({ override: 30 }), 30, "B7 华沿 override");
eq(speedOf({ speed_pct: 5, override: 30 }), 5, "B8 两个都有时以 speed_pct 为准（埃夫特口径优先）");
eq(speedOf({ speed_pct: 0 }), 0, "B9 速度 0 是真读数，不能被当成\"没有\"而回落 override");
eq(speedOf({ speed_pct: 0, override: 30 }), 0, "B10 同上：0 不回落");
eq(speedOf({}), null, "B11 都没有 → null（界面显示破折号）");
eq(speedOf({ speed_pct: 12.6 }), 13, "B12 四舍五入到整数档");

// ---------------------------------------------------------------------
section("C. statusText：埃夫特");
// ---------------------------------------------------------------------
eq(statusText("EFORT", { read_error: "timeout" }), "状态位读取失败", "C1 读取失败要先说，别拿旧位冒充现状");
eq(statusText("EFORT", { read_error: "x", estop: true }), "状态位读取失败", "C2 读取失败优先于其它位");
eq(statusText("EFORT", { estop: true }), "急停", "C3 急停");
eq(statusText("EFORT", { alarm: true }), "报警", "C4 报警");
eq(statusText("EFORT", { running: true, program: 7 }), "运行中 · 程序 7", "C5 运行中带程序号");
eq(statusText("EFORT", { running: true }), "运行中", "C6 运行中无程序号");
eq(statusText("EFORT", { running: true, program: 0 }), "运行中 · 程序 0", "C7 程序号 0 是真值，不能省掉");
eq(statusText("EFORT", { servo_on: false }), "伺服未上电", "C8 伺服没上电");
eq(statusText("EFORT", { servo_on: true, auto: true, remote: true }), "就绪 · 自动/远程", "C9 自动+远程");
eq(statusText("EFORT", { servo_on: true }), "就绪 · 手动", "C10 既非自动也非远程 → 手动");
eq(statusText("efort", { estop: true }), "急停", "C11 brand 小写也认（后端大小写口径不保证）");

// ---------------------------------------------------------------------
section("D. statusText：华沿 ELFIN");
// ---------------------------------------------------------------------
eq(statusText("HUAYAN", { error_code: 1023, error_axis: 2 }), "报警 E1023 · J2", "D1 报警带轴号");
eq(statusText("HUAYAN", { error_code: 42 }), "报警 E42", "D2 报警无轴号");
eq(statusText("HUAYAN", { moving: true, enabled: true }), "运动中", "D3 运动中优先于就绪");
eq(statusText("HUAYAN", { enabled: true, inpos: true }), "就绪 · 已到位", "D4 使能 + 到位");
eq(statusText("HUAYAN", { enabled: true, inpos: false }), "已使能", "D5 使能但没到位（还在走）");
eq(statusText("HUAYAN", { enabled: false }), "未使能", "D6 未使能");
eq(statusText("HUAYAN", { via: "ReadActCoord" }), "就绪 · ReadActCoord",
   "D7 兜底通道只有坐标没有状态位 —— 必须把通道名说出来，别伪装成完整状态");
eq(statusText("HUAYAN", {}), "未使能", "D8 空状态位 → 未使能（保守）");
eq(statusText("huayan", { moving: true }), "运动中", "D9 brand 小写也认");

// ---------------------------------------------------------------------
section("E. statusText：认不出的牌子不许瞎编");
// ---------------------------------------------------------------------
eq(statusText("KUKA", { alarm: true }), "状态未知", "E1 未知牌子 + 有字段 → 状态未知（不套任何一家的话术）");
eq(statusText("", {}), "还没读到状态位", "E2 未知牌子 + 无字段 → 还没读到");
eq(statusText(undefined, null), "还没读到状态位", "E3 全空不炸");

// ---------------------------------------------------------------------
section("F. ageText / fmtNum：读数缺口的显示口径");
// ---------------------------------------------------------------------
eq(ageText(0), "刚刚", "F1 0ms → 刚刚");
eq(ageText(999), "刚刚", "F2 999ms → 刚刚");
eq(ageText(1000), "1 秒前", "F3 1000ms → 1 秒前");
eq(ageText(59999), "59 秒前", "F4 59.999s → 59 秒前");
eq(ageText(60000), "1 分钟前", "F5 60s → 1 分钟前");
eq(ageText(Infinity), "—", "F6 从没来过帧 → 破折号");
eq(ageText(NaN), "—", "F7 NaN → 破折号");
eq(ageText(-5), "—", "F8 负数（时钟回拨）→ 破折号，不显示\"-5 秒前\"");
// ★ 这条是"假 0"的钉子：还没读到的轴显示 0.00，现场会以为机器人真在零位。
eq(fmtNum(null), "—", "F9 null → 破折号");
eq(fmtNum(undefined), "—", "F10 undefined → 破折号");
eq(fmtNum(NaN), "—", "F11 NaN → 破折号");
eq(fmtNum("abc"), "—", "F12 脏字符串 → 破折号");
eq(fmtNum(0), "0.00", "F13 真 0 照实显示 0.00");
eq(fmtNum(-0.001, 2), "-0.00", "F14 负零按 toFixed 原样（不做美化，避免与 3D 读数不一致）");
eq(fmtNum(12.3456, 2), "12.35", "F15 两位小数");
eq(fmtNum(1234.5, 1), "1234.5", "F16 一位小数");

// ---------------------------------------------------------------------
section("F2. autoOf：示教器档位（AUTO / 手动 / 没报）");
// ---------------------------------------------------------------------
// ★ 这条是"不许把没数据说成手动"的钉子：现场看到"手动"就会去拧示教器旋钮，
//   而真正的原因可能是这台压根没连上 —— 那是白折腾一趟，还会怀疑大屏不准。
eq(autoOf({ auto: true }), true, "F2-1 埃夫特 auto=true → AUTO");
eq(autoOf({ auto: false }), false, "F2-2 埃夫特 auto=false → 手动");
eq(autoOf({ auto: 1 }), true, "F2-3 数字 1 算 AUTO（后端按 Modbus 位上报的是 0/1）");
eq(autoOf({ auto: 0 }), false, "F2-4 数字 0 算手动");
eq(autoOf({ auto_mode: true }), true, "F2-5 华沿字段是 auto_mode");
eq(autoOf({}), null, "F2-6 两个字段都没有 → null（显示成\"档位 —\"，不是手动）");
eq(autoOf({ auto: null }), null, "F2-7 显式 null → null");
eq(autoOf({ auto: "" }), null, "F2-8 空串 → null");
eq(autoOf({ auto: "0" }), false, "F2-9 字符串 \"0\" 是手动，不是 AUTO（别被真值判断骗了）");
eq(autoOf(null), null, "F2-10 null 不炸");

// ---------------------------------------------------------------------
section("F3. faultOf / linkLabel：报错要带码，链路态不许含糊");
// ---------------------------------------------------------------------
// 现场报修只说"那台红了"没用，必须报出码；而"读不到状态位"也不能显示成"正常"。
eq(faultOf("HUAYAN", { error_code: 1023, error_axis: 2 }), { code: "E1023 · J2", text: "控制器报警" },
   "F3-1 华沿报警带轴号");
eq(faultOf("HUAYAN", { error_code: 42 }), { code: "E42", text: "控制器报警" }, "F3-2 华沿报警无轴号");
eq(faultOf("HUAYAN", { error_code: 0 }), null, "F3-3 码 0 = 没有故障");
eq(faultOf("HUAYAN", { error_code: "0" }), null, "F3-4 字符串 \"0\" 也不算故障（别被真值判断骗了）");
eq(faultOf("EFORT", { estop: true }), { code: "E-STOP", text: "急停按下" }, "F3-5 埃夫特急停");
eq(faultOf("EFORT", { alarm: true }), { code: "ALARM", text: "控制器报警" }, "F3-6 埃夫特报警");
eq(faultOf("EFORT", { read_error: "timeout" }), { code: "LINK", text: "状态位读取失败" },
   "F3-7 读不到状态位要报 LINK，不能显示成正常");
eq(faultOf("EFORT", { servo_on: true, auto: true }), null, "F3-8 正常就绪 → null");
eq(faultOf("KUKA", { error_code: 7 }), { code: "E7", text: "控制器报警" }, "F3-9 认不出的牌子有码就照报");
eq(faultOf("KUKA", {}), null, "F3-10 认不出的牌子没码 → 不下结论");
eq(faultOf("EFORT", null), null, "F3-11 null 状态不炸");
eq(linkLabel(true, false), "在线", "F3-12 在线");
eq(linkLabel(false, false), "离线", "F3-13 离线");
eq(linkLabel(false, true), "仿真", "F3-14 沙盘驱动中的离线台 → 仿真（既不是说谎的在线，也不是误导的离线）");
// ---------------------------------------------------------------------
section("G. makeCard：一台机器人的完整卡片");
// ---------------------------------------------------------------------
const ENTRY = {
  id: "Alower-1", name: "A 下 1", zone: "A", group: "lower", slot: 1, order: 1,
  brand: "EFORT", model: "er8_700h", host: "192.168.1.21", port: 502,
};
const NOW = 1_800_000_000_000;

const c1 = makeCard(ENTRY, [1.234, -20.5, 0, 90, 0, 0],
  { online: true, at: NOW - 400, tcp: [512.345, -1.2, 730.9], status: { servo_on: true, auto: true, speed_pct: 5 } },
  "safe", { now: NOW, modelLabel: "ER8-700H", unverified: false });
eq(c1.id, "Alower-1", "G1 id 原样");
eq(c1.hostPort, "192.168.1.21:502", "G2 host:port 拼好（卡片直接显示）");
eq(c1.q, [1.23, -20.5, 0, 90, 0, 0], "G3 六轴两位小数");
eq(c1.tcp, [512.3, -1.2, 730.9], "G4 TCP 一位小数");
eq(c1.level, "safe", "G5 等级");
eq(c1.statusText, "就绪 · 自动", "G6 状态文案");
eq(c1.speed, 5, "G7 速度档");
eq(c1.ageText, "刚刚", "G8 更新时间");
eq(c1.frozen, false, "G9 400ms 前的帧不算冻结");
eq(c1.modelLabel, "ER8-700H", "G10 型号显示名来自 kinematics.display_name");
eq(c1.unverified, false, "G11 未标记待核");
eq(makeCard(ENTRY, null, { online: true, at: NOW, status: { alarm: true } }, "safe", { now: NOW }).fault,
   { code: "ALARM", text: "控制器报警" }, "G11f 卡片带出故障对象（码 + 一句话）");
eq(makeCard(ENTRY, null, null, "safe", { now: NOW }).fault, null, "G11g 离线卡片没有故障可报");
eq(makeCard(ENTRY, null, null, "safe", { now: NOW, simDriven: true }).linkLabel, "仿真",
   "G11h 沙盘驱动中的台子链路态显示「仿真」");
// 登记名是"A区-下组-1号(最前)"，12 张卡并排时会被省略号切成"A区-下组-…"读不出是哪一台
eq(c1.short, "A下1", "G11b 卡片抬头用短代号（区+排+序号），完整名留在 title 与详情里");
eq(shortName({ zone: "B", group: "upper", slot: 3 }), "B上3", "G11c 短代号：B 区上排 3 号");
eq(shortName({ zone: "C", group: "middle", slot: 2 }), "C2", "G11d 预期外的 group → 只给区+序号，不硬编一个排字");
eq(shortName({}), "?", "G11e 全空也不炸、不显示 undefined");

// 帧停了但 online 还是 true（后端卡死 / 半开链路）—— 必须看得出来
const c2 = makeCard(ENTRY, null, { online: true, at: NOW - 9000, status: {} }, "safe", { now: NOW });
eq(c2.frozen, true, "G12 9 秒没帧 → frozen（大屏上别假装还活着）");
eq(c2.ageText, "9 秒前", "G13 ageText 跟着走");
eq(c2.q, [null, null, null, null, null, null], "G14 没有姿态帧 → 六轴全 null（显示破折号，不是 0）");
eq(c2.tcp, null, "G15 没有 tcp → null");
eq(fmtNum(c2.q[0], 2), "—", "G16 卡片上这一轴显示破折号");

// 从没来过任何帧
const c3 = makeCard(ENTRY, null, null, "safe", { now: NOW });
eq(c3.online, false, "G17 无快照 → 离线");
eq(c3.level, "offline", "G18 无快照 → offline");
eq(c3.ageText, "—", "G19 无快照 → 更新时间破折号");
eq(c3.statusText, "离线 · 无数据", "G20 无快照 → 直说离线，不套牌子话术");
// 卡片上的档位与速度两格：离线时必须是 null（界面显示破折号），不能凭空写"手动"或"0%"
eq(c3.auto, null, "G21a 离线卡片 → 档位未知（不是\"手动\"）");
eq(c3.speed, null, "G21b 离线卡片 → 速度未知（不是 0%）");
const cAuto = makeCard(ENTRY, null,
  { online: true, at: NOW, status: { servo_on: true, auto: true, remote: true, speed_pct: 5 } },
  "safe", { now: NOW });
eq(cAuto.auto, true, "G21c 在线且打到 AUTO 的卡片 → auto=true");
eq(cAuto.speed, 5, "G21d 在线卡片带出速度档 5%");
// ★ 这条钉的是最容易犯的错：在线但状态位是空的，efortText({}) 会返回"伺服未上电"
//   —— 我们根本没读到伺服位，那是编的。大屏上这行字是唯一的安全出口。
const c3b = makeCard(ENTRY, [0, 0, 0, 0, 0, 0], { online: true, at: NOW, status: {} }, "safe", { now: NOW });
eq(c3b.statusText, "在线 · 还没读到状态位", "G20b 在线但状态位为空 → 不谎报\"伺服未上电\"");
eq(statusText("EFORT", {}), "伺服未上电", "G20c 对照：statusText 本身对空位是这么说的（所以 makeCard 必须先拦一道）");

// 华沿 + 机型待核 + 报警
const HY = { ...ENTRY, id: "Bupper-3", brand: "HUAYAN", model: "elfin_e3", zone: "B", group: "upper", slot: 3 };
const c4 = makeCard(HY, [0, 0, 0, 0, 0, 0],
  { online: true, at: NOW, status: { error_code: 1023, error_axis: 2 }, cart: [1, 2, 3, 4, 5, 6] },
  "hit", { now: NOW, modelLabel: "ELFIN E3", unverified: true });
eq(c4.level, "alarm", "G21 华沿报警 + 围栏 hit → alarm（报警优先）");
eq(c4.statusText, "报警 E1023 · J2", "G22 华沿文案");
eq(c4.unverified, true, "G23 机型待核标记透传（华沿型号是靠外形推的）");
eq(c4.cart, [1, 2, 3, 4, 5, 6], "G24 控制器笛卡尔 6 个数");
const c5 = makeCard({ ...HY, model: "x" }, null, { online: true, at: NOW, status: {} }, "safe", { now: NOW });
eq(c5.modelLabel, "x", "G25 没有 display_name 时回落型号键名（不显示空白）");

// ---------------------------------------------------------------------
section("H. fleetBanner：12 台里必须先说出\"哪一台\"");
// ---------------------------------------------------------------------
const card = (id, name, level, extra = {}) => ({
  id, name, short: extra.short || name, level,
  online: level !== "offline", statusText: extra.statusText || "就绪", ...extra,
});
const SUMMARY = { total: 12, online: 11, alarm: 1, moving: 2 };

eq(fleetBanner([], SUMMARY), null, "H1 没有卡片 → 不显示横条");
eq(fleetBanner(null, SUMMARY), null, "H2 null → 不显示");
eq(fleetBanner([card("a", "A 下 1", "safe"), card("b", "B 下 1", "offline")], SUMMARY), null,
   "H3 全安全 + 有离线 → 不报警（离线在 KPI 里说，不该抢横条）");
eq(fleetBanner([card("a", "A 下 1", "warn")], SUMMARY).state, "warn", "H4 warn → 横条 warn 配色");

const bHit = fleetBanner([card("a", "A 下 1", "safe"), card("h", "B 上 3", "hit")], SUMMARY);
eq(bHit.state, "hit", "H5 hit → hit 配色");
ok(bHit.text.includes("B 上 3"), "H6 横条里必须点名是哪一台（只说\"危险\"等于没说）");

const bAlarm = fleetBanner([card("w", "A 下 2", "warn", { short: "A下2" }),
  card("x", "B 下 1", "hit", { short: "B下1" }),
  card("y", "A 上 1", "alarm", { short: "A上1", statusText: "报警 E1023 · J2" })], SUMMARY);
eq(bAlarm.state, "hit", "H7 CSS 只有 safe/warn/danger/hit 四档 → alarm 借用 hit（最刺眼的红）");
ok(bAlarm.text.startsWith("机房报警：A上1"), "H8 多台出事时报最严重那台：" + bAlarm.text);
ok(bAlarm.text.includes("等 3 台"), "H9 说清一共几台：" + bAlarm.text);
ok(bAlarm.sub.includes("在线 11/12"), "H10 副行给汇总（在线/报警/运动/离线）");

const bNoSummary = fleetBanner([card("z", "A 下 1", "danger")], null);
ok(bNoSummary.sub.includes("在线 1/1"), "H11 没有后端汇总时自己数卡片：" + bNoSummary.sub);
const bOfflineOnly = fleetBanner([card("o", "A 下 1", "offline")], { total: 1, online: 0 });
eq(bOfflineOnly, null, "H12 只有离线 → 不报（离线不是安全事件）");

// ---------------------------------------------------------------------
section("I. 组件层守卫：大屏两块 SFC 只准用设计令牌");
// ---------------------------------------------------------------------
// index.html 的 :root 注释写着"组件里只准引用变量，不准写 #hex / rgba()"。
// 大屏是新增的最大一块视觉面，最容易顺手写死颜色 —— 一旦写死，
// 主题一改这块就跟不上，而 12 张卡的等级色是安全相关的显示。
{
  const files = ["src/components/FleetView.vue", "src/components/FleetCard.vue"];
  const tokens = fs.readFileSync(path.join(ROOT, "index.html"), "utf8");
  const defined = new Set([...tokens.matchAll(/^\s*(--[a-z0-9-]+)\s*:/gm)].map((m) => m[1]));
  ok(defined.size > 40, `I0 index.html 的 :root 解析出 ${defined.size} 个设计令牌`);
  const bad = [];
  for (const rel of files) {
    const src = fs.readFileSync(path.join(ROOT, rel), "utf8");
    const style = src.match(/<style[^>]*>([\s\S]*?)<\/style>/);
    if (!style) { bad.push(`${rel}: 没有 <style> 块`); continue; }
    for (const m of style[1].matchAll(/#[0-9a-fA-F]{3,8}\b|rgba?\(/g)) {
      bad.push(`${rel}: ${m[0]}`);
    }
    // 变量必须真的存在（写错名字 → var() 静默失效 → 元素变透明/黑块）。
    // 组件自己声明的局部属性（如状态环的 --pct，由 :style 注入）算已定义。
    const local = new Set([...style[1].matchAll(/(--[a-z0-9-]+)\s*:/g)].map((m) => m[1]));
    for (const m of style[1].matchAll(/var\((--[a-z0-9-]+)/g)) {
      if (!defined.has(m[1]) && !local.has(m[1])) bad.push(`${rel}: 用了未定义的 ${m[1]}`);
    }
  }
  eq(bad, [], "I1 FleetView/FleetCard 无硬编码色，且所有 var(--x) 都在 :root 里定义过");
}

console.log(`\n结果: ${pass} passed / ${fail} failed  → ${fail ? "FAIL" : "PASS"}`);
process.exit(fail ? 1 : 0);
