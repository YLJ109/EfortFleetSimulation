/**
 * headless 回归：机房设置页的比对与校验（src/utils/fleetRegistry.js）
 *
 * 为什么值得钉：这一层决定"点保存之后到底把哪几台、哪几个字段发出去"。
 * 它错了不会崩，而是**改错机器** —— 比如把没动过的行也发上去（稀疏覆盖层会被
 * 后端按"等于默认值"丢掉，于是显示成没改过），或者编号撞车没提前拦住，
 * 让操作员在机房里对着一个 400 才反应过来。
 *
 * 运行：node tools/verify_fleet_registry.mjs   （已挂在 npm run verify）
 */
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..");

const {
  EDITABLE, ipOk, diffRows, diffLayout, validateRows, rowTouched,
} = await import(pathToFileURL(path.join(ROOT, "src", "utils", "fleetRegistry.js")).href);

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

const ROW = (over = {}) => ({
  id: "A1-1", name: "A区-下组-1号(最前)", host: "192.168.1.12", port: 502,
  model: "efort_er8_700h", brand: "EFORT", zone: "A", group: "lower", slot: 1,
  order: 1, overridden: [], ...over,
});
const ROW2 = (over = {}) => ROW({ id: "A1-2", name: "A区-下组-2号", host: "192.168.1.13", order: 2, slot: 2, ...over });
const META = {
  models: [{ key: "efort_er8_700h", display_name: "埃夫特 ER8-700H" },
    { key: "huayan_elfin", display_name: "华沿 ELFIN 协作臂" }],
  zones: ["A", "B"], groups: ["lower", "upper"], group_size: 3, name_max: 40,
  layout: { cell_pitch_m: 1.64, group_gap_m: 1, zone_gap_m: 1, yaw_deg: 0 },
};

// ---------------------------------------------------------------------
section("A. IPv4 判据（后端同规则，这里只图少跑一趟）");
ok(ipOk("192.168.1.12"), "A1 常规地址");
ok(ipOk("0.0.0.0") && ipOk("255.255.255.255"), "A2 边界 0 与 255 都算合法");
ok(ipOk(" 10.0.0.1 "), "A3 首尾空白自己吃掉，不算非法");
ok(!ipOk("256.1.1.1"), "A4 一段超 255 要拒");
ok(!ipOk("1.2.3"), "A5 少一段要拒");
ok(!ipOk("1.2.3.4.5"), "A6 多一段要拒");
ok(!ipOk("192.168.1."), "A7 结尾缺点要拒");
ok(!ipOk("") && !ipOk(null) && !ipOk(undefined), "A8 空值不是地址（空串另由必填判）");
ok(!ipOk("localhost") && !ipOk("192.168.1.a"), "A9 主机名/字母一律拒 —— 登记表只收 IPv4");

// ---------------------------------------------------------------------
section("B. 草稿比对：只把真改过的字段发出去");
{
  const r = ROW();
  eq(diffRows([r], {}), [], "B1 没草稿 = 没改动");
  eq(diffRows([r], { "A1-1": {} }), [], "B2 空草稿 = 没改动");
  eq(diffRows([r], { "A1-1": { host: "10.0.0.9" } }), [{ id: "A1-1", host: "10.0.0.9" }],
    "B3 只发改过的那一个字段");
  eq(diffRows([r], { "A1-1": { host: r.host } }), [], "B4 填回原值不算改动");
  eq(diffRows([r], { "A1-1": { host: "  " } }), [{ id: "A1-1", host: "  " }],
    "B5 清空是改动（必须发出去，让后端报\"IP 写法不对\"，不能静默忽略）");
  eq(diffRows([r], { "A1-1": { name: "  新名字  " } }), [{ id: "A1-1", name: "  新名字  " }],
    "B6 值原样发（trim 由后端做），但比对时按 trim 判等");
  eq(diffRows([r], { "A1-1": { name: "新名字" } }), [{ id: "A1-1", name: "新名字" }], "B7 改名");
  ok(diffRows([r], { "A1-1": { port: "502" } }).length === 0, "B8 端口字符串 \"502\" == 数字 502，不算改动");
  ok(diffRows([r], { "A1-1": { zone: "a" } }).length === 0, "B9 zone 大小写归一，不算改动");
  ok(diffRows([r], { "A1-1": { group: "LOWER" } }).length === 0, "B10 group 大小写归一，不算改动");
  ok(diffRows([r], { "A1-1": { order: "1" } }).length === 0, "B11 编号字符串同样按数值判等");
  ok(diffRows([r], { "A1-1": { id: "B9-9" } }).length === 0, "B12 想改 id 也发不出去（不在可改字段里）");
  eq(diffRows([r], { "ZZ-9": { host: "10.0.0.1" } }), [], "B13 草稿里有张不存在的 id → 忽略");
  const many = diffRows([r], { "A1-1": { host: "10.0.0.9", port: 1502, name: r.name } });
  eq(many, [{ id: "A1-1", host: "10.0.0.9", port: 1502 }], "B14 多字段一起发，未改的不带");
  ok(rowTouched(r, { "A1-1": { host: "10.0.0.9" } }) === true, "B15 rowTouched 认改动");
  ok(rowTouched(r, { "A1-1": { host: r.host } }) === false, "B16 rowTouched 不把原值当改动");
}
ok(EDITABLE.join(",") === "name,host,port,model,zone,group,slot,order",
  `B17 可改字段与后端 ROBOT_FIELDS 同源（当前 ${EDITABLE.join("/")}）`);

// ---------------------------------------------------------------------
section("C. 阵列改动");
eq(diffLayout(META.layout, {}), {}, "C1 空草稿");
eq(diffLayout(META.layout, { cell_pitch_m: 1.64 }), {}, "C2 原值不发");
eq(diffLayout(META.layout, { cell_pitch_m: "1.9" }), { cell_pitch_m: 1.9 }, "C3 字符串数值化后发出");
eq(diffLayout(META.layout, { group_gap_m: "" }), {}, "C4 空串 = 没填，不发");
eq(diffLayout(META.layout, { yaw_deg: "abc" }), { yaw_deg: "abc" }, "C5 非数字照样发，交给校验报错（不能吞）");
eq(diffLayout(null, { zone_gap_m: 2 }), { zone_gap_m: 2 }, "C6 基准缺失时视为改动");

// ---------------------------------------------------------------------
section("D. 前置校验：一次报全");
{
  const bad = { "A1-1": { host: "300.1.1.1", port: 0, slot: 9, zone: "C", group: "mid", name: "  ", model: "x", order: 0 } };
  const e = validateRows([ROW()], bad, META);
  const fields = e.map((x) => x.field).sort();
  eq(fields, ["group", "host", "model", "name", "order", "port", "slot", "zone"].sort(),
    "D1 每个非法字段各报一条，不修一个弹一个");
  ok(e.every((x) => x.id === "A1-1"), "D2 错误点名的就是被改的那台");
}
{
  const e = validateRows([ROW(), ROW2()], { "A1-1": { order: 2 } }, META);
  ok(e.some((x) => x.field === "order" && /A1-2/.test(x.msg)),
    "D3 编号撞上**没被改动**的那台也要拦住（跨行判据）");
}
{
  // 真交换：两台互相拿对方的编号。按"草稿生效后的全场"判 → 不该报错
  const e = validateRows([ROW(), ROW2()], { "A1-1": { order: 2 }, "A1-2": { order: 1 } }, META);
  eq(e.filter((x) => x.field === "order"), [], "D4 整体换号是合法操作，不能被误判成撞车");
}
{
  const e = validateRows([ROW()], { "A1-1": { port: 65536 } }, META);
  ok(e.some((x) => x.field === "port"), "D5 端口上界");
  const e2 = validateRows([ROW()], { "A1-1": { port: "abc" } }, META);
  ok(e2.some((x) => x.field === "port"), "D6 端口非数字要报，不能静默当没填");
  const e3 = validateRows([ROW()], { "A1-1": { slot: 3 } }, META);
  eq(e3, [], "D7 slot 在上界内（每排 3 台）");
  const e4 = validateRows([ROW()], { "A1-1": { name: "x".repeat(41) } }, META);
  ok(e4.some((x) => x.field === "name"), "D8 名称超长");
  const e5 = validateRows([ROW()], { "GHOST": { host: "1.1.1.1" } }, META);
  ok(e5.length === 1 && e5[0].field === "id", "D9 登记表里没有的 id 单独报一条");
  const e10 = validateRows([ROW()], {}, META);
  eq(e10, [], "D10 没草稿就没有错（别把生效值本身当问题）");
}
{
  // meta 缺字段时不能整页报错：回落默认枚举
  const e = validateRows([ROW()], { "A1-1": { zone: "B", group: "upper", slot: 1 } }, {});
  eq(e, [], "D11 meta 空也能判（用 A/B、lower/upper 兜底，且没给 group_size 就不卡 slot 上界）");
}

// ---------------------------------------------------------------------
section("E. 与后端契约一致的兜底");
{
  const r = ROW({ port: null });       // yaml 里没写端口 → 后端补驱动默认后才有值
  ok(r.port === null, "E1 基准行允许端口为空（describe 里已按驱动默认补过）");
  eq(diffRows([r], { "A1-1": { port: 502 } }), [{ id: "A1-1", port: 502 }],
    "E2 空 → 502 算改动（后端稀疏化会自己判断它等于驱动默认，不该由前端瞒着不发）");
  eq(diffRows([r], { "A1-1": { port: "" } }), [],
    "E3 空 → 空 不算改动");
}

console.log(`\n===== 结果: ${pass} passed, ${fail} failed =====`);
process.exit(fail === 0 ? 0 : 1);
