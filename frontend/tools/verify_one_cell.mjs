// =====================================================================
// ★★ E7「一格三证 = 同一格的 照片 / 判色 / 位姿 装订」守卫（headless 静态检查）
//
// 方案：docs/方案-创新功能四批（服务端安全与前瞻·前端可观测·高性能画面·摆放体检）.md §E7
//
// 这条功能的立意是「**数据早就有，缺的是装订**」，所以钉的四条正好是它最容易被
// 做坏的方式：
//   ① **只读**：装订与查询都不许下发运动、不许为凑三证补拍（真机分拣运行期间绝不打断）；
//   ② **不另立第二秩序权威**：格号只有一份派生规则、期望姿态取 `_move` 返回的**实发值**、
//      到位结论取 E2 同源残差、偏差公式用 `sorting.joint_deviation`；
//   ③ **缺就写缺**：任何一证没到手必须有原因，`ok` 不许从"看着有内容"推断出来；
//   ④ **水位单调 + 三态不塌**（§5.5 铁律①③）：前端用 `latest_seq` 而不是数组长度，
//      「未知」必须和「缺」长得不一样。
//
// 运行： cd frontend && node tools/verify_one_cell.mjs
// =====================================================================
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const beRoot = join(root, "..", "backend");
const read = (abs) => readFileSync(abs, "utf8")
  .split(String.fromCharCode(13, 10)).join(String.fromCharCode(10));
const fe = (rel) => read(join(root, rel));
const be = (rel) => read(join(beRoot, rel));

let pass = 0, fail = 0;
function ok(name, cond, extra = "") {
  if (cond) { pass++; console.log("  PASS  " + name); }
  else { fail++; console.log("  FAIL  " + name + (extra ? "   → " + extra : "")); }
}

function stripJs(s) {
  return s.replace(/\/\*[\s\S]*?\*\//g, "")
          .replace(/<!--[\s\S]*?-->/g, "")
          .replace(/^\s*\/\/.*$/gm, "");
}
function stripPy(s) {
  return s.split(String.fromCharCode(10))
          .filter((l) => !/^\s*#/.test(l))
          .join(String.fromCharCode(10))
          .replace(/""".*?"""/gs, "");
}

const MOD = be("app/services/cell_evidence.py");
const API = be("app/api/sorting.py");
const STORE = fe("src/stores/cell.js");
const VIEW = fe("src/components/CellEvidenceCard.vue");
const SORT = fe("src/components/SortingView.vue");
const PKG = fe("package.json");
const TESTS = be("tests/test_cell_evidence.py");
const TTT = be("app/api/tictactoe.py");

const MODC = stripPy(MOD);
const APIC = stripPy(API);
const STOREC = stripJs(STORE);
const VIEWC = stripJs(VIEW);
// 装订函数体（截段：整文件里"分拣"两个字到处都有，不能整片判）
const helper = (API.split("def _cell_evidence(")[1] || "").split("\n\n\n")[0];
const helperC = stripPy(helper);
// 一格三证的两个只读端点（截到「启停」为止）
const cellsEp = (API.split('@router.get("/cells")')[1] || "").split("# ---------------------------------------------------------------- 启停")[0];
const cellEp = (API.split('@router.get("/cells/{cell}")')[1] || "").split("# ---------------------------------------------------------------- 启停")[0];

console.log("\n=== E7 一格三证 ===\n");

// ---------------------------------------------------------------- ① 只读 / 不动作
ok("A1 装订模块不 import 写入路径（motion / modbus / db / camera 客户端）",
   !/import .*motion|from app\.services\.motion|modbus|from app\.db|camera_client/.test(MODC));
ok("A2 装订函数里没有下发 / 拍照 / 读库 / 事件写",
   !/motion\.command|camera_client|request_capture|SessionLocal|list_points|emit_event|_move\(/.test(helperC));
ok("A3 装订函数把异常吞掉并返回 None（观测件坏了不许带走检主线）",
   /except Exception/.test(helper) && /return None/.test(helper));
ok("A4 分拣线程里不新增同步读库：模块声明「不新增读库」且没有 db 调用",
   /新增读库/.test(MOD) && !/get_vision_record|list_vision_records|SessionLocal/.test(MODC));
ok("A5 前端 store 只有 GET（无 POST / 无 apiControl / 无 fetch 带 method）",
   /fetch\(apiUrl\(`\/sorting\/cells/.test(STOREC)
   && !/method:/.test(STOREC) && !/apiControl/.test(STOREC));
ok("A6 卡面没有任何动作按钮（只有「重查」这一个只读按钮）",
   (VIEWC.match(/<button/g) || []).length ===
   (VIEWC.match(/cell\.load\(\)/g) || []).length + (VIEWC.match(/cell\.pick\(/g) || []).length
   && !/exec\.|vacuum|\/sorting\/start|\/sorting\/goto/.test(VIEWC));
ok("A7 照片这一证不为凑齐补拍（拿不到就 ok=False + 原因）",
   /不为了凑齐去补拍/.test(MOD) && /没有补拍/.test(MOD));

// ---------------------------------------------------------------- ② 唯一权威
ok("B1 格号只有一份派生规则（别名也走同一实现，不再拼字符串）",
   /def cell_id\(/.test(MODC) && /return cell_id\(name\)/.test(MODC)
   && !/def cell_of_point\(name[\s\S]{0,200}re\.(match|compile)/.test(MODC));
ok("B2 原位与正上位是同一个格子（`B3` / `B3上` → `B3`）",
   /_SLOT_RE = re\.compile\(r"\^\(\[AB\]\)\(\[1-9\]\)\$"\)/.test(MOD)
   && /_ABOVE_RE = re\.compile\(r"\^\(\[AB\]\)\(\[1-9\]\)上\$"\)/.test(MOD));
ok("B3 前端不自己派生格号（没有第二套 `+ 上` 规则）",
   !/\+ "上"/.test(STOREC) && !/\+ "上"/.test(VIEWC));
ok("B4 期望姿态取 _move 返回的实发目标（单件 + 双面 + 放回三处）",
   (APIC.match(/drop_target = _move\(/g) || []).length === 2
   && /down_target = _move\(/.test(APIC));
ok("B5 到位结论取 E2 同源残差（不自己判到位）",
   /from app\.services\.twin_residual import twin_residual/.test(MOD)
   && /twin_residual\.recent\(/.test(MOD));
ok("B6 偏差公式只有 sorting.joint_deviation 一份（装订模块不写 abs 循环）",
   /S\.joint_deviation\(/.test(MOD) && !/abs\(av\[/.test(MODC));
ok("B7 偏差公式的实现确在 sorting（J6 环形差）",
   /def joint_deviation\(a: Any, b: Any\)/.test(be("app/services/sorting.py")));
ok("B8 三证名字是一个固定元组（界面/测试按它逐条核对，不许改名）",
   /EVIDENCE_KEYS: Tuple\[str, str, str\] = \("photo", "colour", "pose"\)/.test(MOD));
ok("B9 残差带曲线不进三证（同屏同源，要看曲线查 E2）",
   /RESIDUAL_KEYS = \([\s\S]*?\)/.test(MOD)
   && !/profile/.test((MOD.split("RESIDUAL_KEYS = (")[1] || "").split(")")[0]));

// ---------------------------------------------------------------- ③ 缺就写缺
ok("C1 `ok` 只认调用方明确写下的结论（不猜）",
   /bool\(row\.get\("ok"\)\) and has/.test(MOD) && /`ok` \*\*只认调用方明确写下的结论\*\*/.test(MOD));
ok("C2 缺的证据必须带原因并进 missing",
   /missing\.append\(key\)/.test(MOD) && /row\.setdefault\("reason"/.test(MOD));
ok("C3 照片没有档时如实 ok=False（带「没有照片」这条路的钉）",
   /"ok": False, "images": \{\}/.test(MOD));
ok("C4 没有实时姿态 → 位姿这一证没有（不拿名义姿态冒充）",
   /读不到实时姿态/.test(MOD));
ok("C5 没有残差 → 不能说到到位",
   /不能说到到位/.test(MOD));
ok("C6 档与记录差太久不硬凑（时间窗存在）",
   /_PHOTO_WINDOW_S/.test(MOD) && /不算同一件事/.test(MOD));
ok("C7 空跑如实标 simulated（不假装真机到位）",
   /simulated=bool\(dry\)/.test(helper));
ok("C8 非料格点位如实跳过装订（不硬造格子）",
   /if not cell:\s*\n\s*return None/.test(helper));
ok("C9 空跑占位色不当判色证据（placeholder → ok=False + 如实写明）",
   /if d\.get\("placeholder"\):/.test(MOD) && /不拿占位色当这一格的判色证据/.test(MOD));

// ---------------------------------------------------------------- ④ 水位与三态
ok("D1 记录带只增不减的 seq（模块内不用数组长度当水位）",
   /def _next_seq\(/.test(MOD) && /rec\["seq"\] = _next_seq\(\)/.test(MOD));
ok("D2 前端水位用 latest_seq，不用 records.length 判新增",
   /d\.latest_seq/.test(STOREC) && !/records\.length\s*[<>]/.test(STOREC));
ok("D3 后端重启水位归零时如实标注（不静默吞掉倒退）",
   /seq < this\.latestSeq/.test(STOREC) && /rebooted/.test(STOREC));
ok("D4 三态不塌：ok===true / ===false / 未知 三条分支都在",
   /ev\.ok === true/.test(VIEWC) && /ev\.ok === false/.test(VIEWC) && /return "unknown"/.test(VIEWC));
ok("D5 「未知」有自己的样式（虚线灰，不与「缺」同色）",
   /v-unknown/.test(VIEW) && /border-style: dashed/.test(VIEW));
ok("D6 读失败写「这不代表没有隐患」（不清空成没事）",
   /这不代表没有隐患/.test(VIEW));
ok("D7 没有记录写「没有记录 ≠ 没有发生」并指向 jsonl",
   /没有记录 ≠ 没有发生/.test(VIEW) && /cells-YYYYMMDD\.jsonl/.test(VIEW));
ok("D8 不假装看得到图（卡里没有 <img>，只列档名）",
   !/<img/.test(VIEW) && /不在本页取图/.test(VIEW));

// ---------------------------------------------------------------- ⑤ 端点：只读、不要令牌
ok("E1 GET /api/sorting/cells 不需要控制令牌（观测不属于控制）",
   /@router\.get\("\/cells"\)\ndef api_cells\([\s\S]{0,200}?\):/.test(API)
   && !/require_control/.test(cellsEp));
ok("E2 单格端点同样只读、不要令牌",
   /@router\.get\("\/cells\/\{cell\}"\)/.test(API) && !/require_control/.test(cellEp));
ok("E3 端点回包带 latest_seq / records / stats / evidence_keys",
   /"latest_seq": CE\.latest_seq\(\)/.test(cellsEp) && /"evidence_keys": list\(CE\.EVIDENCE_KEYS\)/.test(cellsEp));
ok("E4 落盘在后台线程（record 只 append + 入队）",
   /_queue\.put\(rec\)/.test(MODC) && /threading\.Thread\(target=_drain/.test(MOD));
ok("E5 有测试隔离用的 reset()（只清内存，不删已落盘的档）",
   /def reset\(\) -> None:/.test(MOD) && /_recent\.clear\(\)/.test(MOD));

// ---------------------------------------------------------------- ⑥ 接线：三处都装订
ok("F1 单件落料装订（事件带 cell_id + 齐全度）",
   /_cell_evidence\(tgt \+ "上", v=v, target=drop_target/.test(API)
   && /"cell_id": CE\.cell_id\(tgt\)/.test(API));
ok("F2 双面落料装订并标 face（同格两件分得开）",
   /_cell_evidence\(tgt \+ "上", v=v, target=drop_target, dry=dry,\n\s*extra=\{"piece": done \+ 1[\s\S]{0,160}"dual_face": True/.test(API));
ok("F3 放回原位也装订（判不出色的那一格最需要证据）",
   /_cell_evidence\(src_slot, v=v, target=down_target/.test(API));
ok("F4 _put_back 的判色证据由调用方传入，不传就如实记「没有判色」",
   /v: Optional\[Dict\] = None/.test(API) && /"这一格没有判色/.test(MOD));
ok("F5 井字棋放回也把实判结论传进去（不编结论）",
   (TTT.match(/prefix="放回原位 %s" % src_slot, st=st, face=face, v=v/g) || []).length === 2);
ok("F6 卡挂到智能分拣页（有 import + 有渲染）",
   /import CellEvidenceCard from "\.\/CellEvidenceCard\.vue"/.test(SORT)
   && /<CellEvidenceCard \/>/.test(SORT));

// ---------------------------------------------------------------- ⑦ 回归钉本身在
ok("G1 后端有 test_cell_evidence.py 且覆盖格号派生 / 缺就写缺 / 只读端点",
   /def test_slot_and_its_above_point_share_one_cell/.test(TESTS)
   && /def test_bind_never_infers_ok_from_content/.test(TESTS)
   && /def test_cells_endpoint_is_readonly_without_control_token/.test(TESTS));
ok("G2 测试有 autouse 清理（内存历史是进程级的，不清就串味）",
   /@pytest\.fixture\(autouse=True\)[\s\S]{0,200}CE\.reset\(\)/.test(TESTS));
ok("G3 本脚本已并入 npm run verify",
   /verify_one_cell\.mjs/.test(PKG));
ok("G4 模块自带「刻意不做」的边界声明（文档即钉子）",
   /绝不在分拣热路径里新增拍摄/.test(MOD) && /不裁决、不推断/.test(MOD));

console.log(`\n一格三证（E7）守卫：${pass} 过 / ${fail} 败`);
if (fail) process.exit(1);
