// =====================================================================
// ★ E4「现场自学习判色闭环」守卫（headless 静态检查）
// 方案：docs/方案-创新功能四批（服务端安全与前瞻·前端可观测·高性能画面·摆放体检）.md §E4
//
// 这条链子的价值 = "阈值由现场证据长出来，但**每一步都要人签字**"。
// 所以要钉死的是四种"看起来很智能、其实会出事"的写法：
//   ① 观测件长出手：样本环/建议面去调相机下发或运动通道 → 分拣跑到一半被改阈值；
//   ② 假显示：把"现场生效值不知道"渲染成出厂默认、把"没样本"渲染成 0；
//   ③ 学习填标定：样本攒够了就把"未标定"的橙/黄/青/紫说成已标定；
//   ④ 建议=生效：界面上出现"已应用/已生效"字样，或前端偷偷走 /vision/config。
//
// 运行： cd frontend && node tools/verify_color_advice.mjs
// =====================================================================
import { readFileSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const repo = join(root, "..");
const read = (p) => readFileSync(p, "utf8").split(String.fromCharCode(13, 10)).join(String.fromCharCode(10));
const fe = (rel) => read(join(root, rel));
const be = (rel) => read(join(repo, "backend", rel));

let pass = 0, fail = 0;
function ok(name, cond, extra = "") {
  if (cond) { pass++; console.log("  PASS  " + name); }
  else { fail++; console.log("  FAIL  " + name + (extra ? "   → " + extra : "")); }
}
function group(t) { console.log(String.fromCharCode(10) + "— " + t); }

/** 剥掉块注释 / HTML 注释 / 整行 // —— 注释里写"不下发"不该把静态钉判红。 */
function strip(s) {
  return s.replace(/\/\*[\s\S]*?\*\//g, "")
          .replace(/<!--[\s\S]*?-->/g, "")
          .replace(/^\s*\/\/.*$/gm, "");
}

const CARD = strip(fe("src/components/ColorAdviceCard.vue"));
const STORE = strip(fe("src/stores/colorAdvice.js"));
const OPS = strip(fe("src/components/EventsView.vue"));
const SORT = strip(be("app/api/sorting.py"));
const SAMPLES = strip(be("app/services/vision_samples.py"));
const ADV = strip(be("app/services/color_advice.py"));
const VAPI = strip(be("app/api/vision.py"));

group("A 接线：样本与判色**同源同批**");
ok("A1 sorting 里唯一登记口 = _sample_record（不在别处直接 ingest）",
  (SORT.match(/VS\.ingest\(/g) || []).length === 1
  && /def _sample_record\(/.test(SORT));
ok("A2 单件判色调用 _sample_record（帧与结论同批）", /_sample_record\(got, v, stage=stage/.test(SORT));
ok("A3 双面判色每面各登记一条（不合成一条）",
  /for _slot, _grp, _v in zip\(list\(faces\), groups, vs\):\s*\n\s*_sample_record\(/.test(SORT));
ok("A4 登记口只吞异常不带崩主线（except 后只 log.debug）",
  /def _sample_record\([\s\S]{0,900}except Exception[\s\S]{0,160}log\.debug/.test(SORT));
ok("A5 送检发生在 `judge` 为真的分支里（空跑不调相机、也不登记）",
  SORT.includes('v = _capture(ctx, int(body.frames), min_conf, "判色"')
  && /if not judge:/.test(SORT));

group("B 观测件不许长出手（后端）");
/** 取一个函数体（从 `def 名字(` 到下一个顶层 `def ` / `@` 装饰器）。 */
function fn(src, name) {
  const i = src.indexOf("def " + name + "(");
  if (i < 0) return "";
  const rest = src.slice(i + 1);
  const j = rest.search(/\n(?=def |@|class )/);
  return j < 0 ? rest : rest.slice(0, j);
}
const INGEST = fn(SAMPLES, "ingest") + fn(SAMPLES, "sample_from");
const PROPOSE = fn(VAPI, "api_advice_propose");
const ROLLBACK = fn(VAPI, "api_advice_rollback");
const ADVREAD = fn(VAPI, "api_advice");
const SAMPLES_EP = fn(VAPI, "api_samples");

ok("B1 样本环模块不 import 任何运动/下发通道",
  !/import (app\.services\.(motion|rc_ready|jog)|from app\.services import (motion|jog))/.test(SAMPLES)
  && !/motion\.command|jog\./.test(SAMPLES));
ok("B2 ingest / sample_from 里一次相机 HTTP 都没有（阈值只读进程缓存）",
  !/camera_client/.test(INGEST));
ok("B2b 全模块只有 GET（刷新快照用 /vision/status），绝不 POST 相机",
  !/post_json/.test(SAMPLES));
ok("B3 建议面模块零下发（不调相机也不调运动）",
  !/camera_client|motion\.|post_json/.test(ADV));
ok("B4 像素/mask 结构性挡在表外（白名单里没有 mask/box/rect）",
  /COLOR_KEYS/.test(SAMPLES)
  && !/["']mask["']/.test(fn(SAMPLES, "frame_features")));
ok("B5 propose / rollback 都要控制令牌（写审计链不是浏览）",
  /def api_advice_propose\([\s\S]{0,400}require_control/.test(VAPI)
  && /def api_advice_rollback\([\s\S]{0,240}require_control/.test(VAPI));
ok("B6 读端点（samples / advice）不需要令牌（不登录也该看得见证据）",
  /Depends\(get_db\)/.test(SAMPLES_EP) && !/require_control/.test(SAMPLES_EP)
  && !/require_control/.test(ADVREAD));
ok("B7 建议链的三个端点自己都对相机服务零 POST（生效只由人走既有 /vision/config 面板）",
  !/post_json|get_json/.test(PROPOSE) && !/post_json|get_json/.test(ROLLBACK)
  && /post_json\("\/vision\/config"/.test(VAPI), "既有面板仍在（人工生效口没被删）");

group("C 硬边界：学习不填标定 · 硬阈值不进建议面");
ok("C1 白名单只有三个可调项（结构上发不出别的键）",
  /ADVISABLE_KEYS[^=]*=\s*\("fill_min", "fill_warn", "min_conf"\)/.test(ADV));
ok("C2 黑暗判据这类硬阈值被点名排除",
  /HARD_EXCLUDED_KEYS[^=]*=\s*\("dark_chroma_max", "dark_s_max"\)/.test(ADV)
  && !/dark_chroma_max["'],\s*"to/.test(ADV));
ok("C3 calibrated_by_learning 永远是 False（样本再多也不叫标定）",
  /"calibrated_by_learning": False/.test(ADV));
ok("C4 未标定四色点名（橙/黄/青/紫）",
  /UNCALIBRATED_COLORS[^=]*=\s*\("橙", "黄", "青", "紫"\)/.test(ADV));
ok("C5 建议文案点名必须先跑现场真图回放",
  /E2E_REPLAY\s*=\s*"camera\/tools\/vision_e2e_replay\.py"/.test(ADV)
  && /E2E_REPLAY/.test(fn(ADV, "summarize"))
  && /vision_e2e_replay/.test(ADV) /* NOTE_SURFACE 里有字面路径 */);
ok("C6 结构上没有「已生效」状态：applied 一律 False",
  (ADV.match(/"applied": False/g) || []).length >= 2
  && !/"applied": True/.test(ADV) && !/"applied":\s*bool\(/.test(ADV));

group("D 前端只显示不判定 · 三态不塌");
ok("D1 卡片自己不发请求（唯一读写入口 = store）", !/\bfetch\(/.test(CARD));
ok("D2 store 是唯一带令牌的一侧（写操作走 apiControl）",
  /apiControl\(/.test(STORE) && !/apiControl/.test(CARD));
ok("D3 现场生效值三态：null 渲染成「不知道」，不塌成数字/出厂默认",
  /from === null[\s\S]{0,120}不知道/.test(CARD));
ok("D4 没有把「没样本/没读到」塌成 0 的读法（`|| 0` 只出现在计数补位以外）",
  !/\?\?\s*0\b/.test(CARD) && !/\.value \|\| 0/.test(STORE));
ok("D5 界面上不出现「已生效 / 已应用阈值」这类假显示",
  !/已生效|已应用阈值|阈值已下发/.test(CARD) && /applied 恒为 false|applied 永远 false/.test(CARD));
ok("D6 前端没有任何 /vision/config 写调用（不替人点生效）",
  !/vision\/config/.test(CARD) && !/vision\/config/.test(STORE));
ok("D7 物料色走唯一出口（本卡不复制七色字面量）",
  /materialColor\(/.test(CARD) && !/["']橙["']\s*:\s*["']#/.test(CARD));
ok("D8 判色体检挂在运维审计页（与 B1 指标、E6 日报同一屏）",
  /ColorAdviceCard/.test(OPS));
ok("D9 空跑占位色在界面上如实标注，不参与复核",
  /placeholder/.test(CARD) && /空跑占位色/.test(CARD));
ok("D10 未授权时写按钮禁用并说明怎么拿令牌（不静默失败）",
  /canWrite/.test(CARD) && /requestLogin\(\)/.test(CARD));

group("E 文件在位（钉「接了线」而不是「写了计划」）");
ok("E1 后端：样本环 + 建议派生两个模块都在",
  existsSync(join(repo, "backend/app/services/vision_samples.py"))
  && existsSync(join(repo, "backend/app/services/color_advice.py")));
ok("E2 前端：store + 卡片都在",
  existsSync(join(root, "src/stores/colorAdvice.js"))
  && existsSync(join(root, "src/components/ColorAdviceCard.vue")));
ok("E3 后端测试钉在位（含 propose 不打相机的点名用例）",
  existsSync(join(repo, "backend/tests/test_color_advice.py"))
  && /def test_apply_advice_runs_e2e_replay_first/.test(be("tests/test_color_advice.py")));

ok("E4 本脚本已并入 `npm run verify`（不挂链的守卫永远不会跑 = 等于没钉）",
  /node tools\/verify_color_advice\.mjs/.test(fe("package.json")));

console.log(String.fromCharCode(10) + `合计：PASS ${pass}  FAIL ${fail}`);
process.exit(fail ? 1 : 0);
