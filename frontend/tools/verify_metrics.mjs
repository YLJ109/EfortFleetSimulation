// =====================================================================
// ★ B1「运行指标（可观测性）」守卫（headless 静态检查）
// 方案：docs/方案-创新功能四批（服务端安全与前瞻·前端可观测·高性能画面·摆放体检）.md §4 B1
//
// 这块表的全部价值是"数字可信"，所以要钉死的是本项目反复踩过的四类坑：
//   ① 「没测」被渲染成「0」          → 读数只能出自 store 的 quad()（缺字段回 null 四元组）；
//   ② 数字没有时刻                  → 每条必须"当前值 + 60s 最大 + 采样时刻"三件一起给；
//   ③ 水位用会不再变化的值           → 新鲜度靠 App 的 1s 时钟 beat()，不靠 Date.now() 自比较；
//   ④ 观测件长出手 / 默认吃性能      → 面板只读零下发；浏览器侧采样默认关闭，卸载即停。
//
// 运行： cd frontend && node tools/verify_metrics.mjs
// =====================================================================
import { readFileSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (rel) => readFileSync(join(root, rel), "utf8").split(String.fromCharCode(13, 10)).join(String.fromCharCode(10));

let pass = 0, fail = 0;
function ok(name, cond, extra = "") {
  if (cond) { pass++; console.log("  PASS  " + name); }
  else { fail++; console.log("  FAIL  " + name + (extra ? "   → " + extra : "")); }
}
function group(t) { console.log(String.fromCharCode(10) + "— " + t); }

/** 剥掉块注释 / HTML 注释 / 整行 // —— 注释里写"不自己 fetch"不该把静态钉判红。 */
function strip(s) {
  return s.replace(/\/\*[\s\S]*?\*\//g, "")
          .replace(/<!--[\s\S]*?-->/g, "")
          .replace(/^\s*\/\/.*$/gm, "");
}

const PANEL = strip(read("src/components/MetricsPanel.vue"));
const SAMPLER = strip(read("src/utils/perfSampler.js"));
const STORE = strip(read("src/stores/metrics.js"));
const MGR = strip(read("src/three/manager.js"));
const APP = strip(read("src/App.vue"));
const OPS = strip(read("src/components/EventsView.vue"));
const PKG = read("package.json");

group("A 只读 · 零下发（观测件不许长出手）");
ok("A1 面板里没有任何写接口封装（apiControl / POST / xlsx 上传）",
  !/apiControl/.test(PANEL) && !/method\s*:\s*"POST"/i.test(PANEL));
ok("A2 面板自己不发请求（唯一轮询者是 store）", !/\bfetch\(/.test(PANEL));
ok("A3 采样器不碰网络", !/\bfetch\(|XMLHttpRequest/.test(SAMPLER));
ok("A4 采样器不碰运动链路（不 import manager 之外的三样）",
  !/from "..\/three/.test(SAMPLER) && !/motion|jog|vacuum/i.test(SAMPLER));
ok("A5 渲染循环里的记录只在开关为真时执行（关闭时一行都不跑）",
  /if \(samplerEnabled\(\)\) \{[\s\S]{0,200}recordFrame\(/.test(MGR));
ok("A6 速度口径没有被触碰（不写 40103 / 不回写 ui-speed）",
  !/40103/.test(PANEL + SAMPLER) && !/ui-speed/.test(PANEL + SAMPLER + MGR));
ok("A7 后端指标端点仍是只读 GET（store 只 GET）",
  /apiUrl\("\/system\/metrics"\)/.test(STORE) && !/method/.test(STORE));

group("B 唯一权威（同一屏同一结论必须同源）");
ok("B1 读数唯一入口 = metrics.quad(分组, 键)", /metrics\.quad\(/.test(PANEL));
ok("B2 面板不许自己下钻 snap.groups（那样 ?.value || 0 会把没测写成 0）",
  !/snap\.groups/.test(PANEL) && !/metrics\.snap/.test(PANEL));
ok("B3 文案唯一来源 = store 的 formatQuad（面板不自己拼 value+unit）",
  /formatQuad\(/.test(PANEL) && !/q\.value \+ /.test(PANEL));
ok("B4 单位来自四元组，不在面板硬编码", /q\.unit/.test(PANEL) && !/"\s*个"\s*:/.test(PANEL));
ok("B5 分组清单与后端一一对应（六个分组全在）",
  ["collector", "modbus", "ws", "runs", "vision", "events"].every((g) => PANEL.includes('"' + g + '"')));
ok("B6 后端新增字段照样显示（不藏：LABEL[key] || key）", /LABEL\[key\] \|\| key/.test(PANEL));

group("C 缺就写缺（铁律③：未知不得塌成布尔/0）");
ok("C1 store 的空四元组 value 一定是 null（不是 0、不是空串）",
  /function emptyQuad[\s\S]{0,240}value: null/.test(STORE));
ok("C2 后端没这个字段时 note 如实写明是哪种缺法",
  /后端版本不含此项/.test(STORE));
ok("C3 面板把没采到写成「还没采到样本」而不是 0", /还没采到样本/.test(PANEL));
ok("C4 非数值项不比大小（写「不是数值」，不假装能算最大）", /不是数值/.test(PANEL));
ok("C5 全表没有 `|| 0` 兜底读数", !/\|\|\s*0/.test(PANEL));
ok("C6 取不到数时写「这块表还没有数」，不写「一切正常」", /这块表还没有数/.test(PANEL));
ok("C7 过期不抹掉数值但必须改写 note（带着旧值不声明是旧的 = 假显示）",
  /数据已过期/.test(STORE));

group("D 水位与新鲜度（铁律①：不许用不再变化的值做判据）");
ok("D1 stale 判据用外部时钟 tick（getter 里不许直接 Date.now 自比较）",
  /this\.tick - this\.at/.test(STORE) && !/getters[\s\S]{0,900}Date\.now\(\) - this\.at/.test(STORE));
ok("D2 App 的 1s 时钟无条件推进 beat()", /metrics\.beat\(\)/.test(APP));
ok("D3 App 启停轮询（隐藏即停，多标签页不各打各的）",
  /metrics\.start\(\)/.test(APP) && /metrics\.stop\(\)/.test(APP));
ok("D4 停帧秒数取自单调时钟差（performance.now，不用墙钟）",
  /performance\.now\(\)/.test(SAMPLER) && !/Date\.now\(\)\s*-\s*last/.test(SAMPLER));
ok("D5 60s 窗口的入窗签名含「值 + 时刻」（漏一个就会重复叠样本 / 漏更新）",
  /const sig = `\$\{v\}\|\$\{tMs\}`/.test(PANEL));
ok("D6 成功轮询计数 seq 单调递增（不是布尔、不是数组长度）", /this\.seq \+= 1/.test(STORE));

group("E 默认关闭 · 开销可见");
ok("E1 采样器默认关", /let enabled = false/.test(SAMPLER));
ok("E2 面板卸载时若仍开着就关掉（没人看的仪表不长期吃性能）",
  /onBeforeUnmount\(\(\) => \{[\s\S]{0,260}stopSampler\(\)/.test(PANEL));
ok("E3 开销本身要显示（帧数 / 窗口 / renderer.info 抽样节拍）",
  /采样器自身/.test(PANEL) && /frames/.test(SAMPLER) && /% 30/.test(MGR));
ok("E4 帧时缓冲是定长环形（不无界增长）",
  /new Float32Array\(CAP\)/.test(SAMPLER) && /\(head \+ 1\) % CAP/.test(SAMPLER));
ok("E5 分位数只在被读取时算（渲染热路径里不排序）",
  !/\.sort\(\)/.test(MGR) && /Float32Array\.from\(w\)\.sort\(\)/.test(SAMPLER));
ok("E6 JS heap 缺失显示不可用（只有 Chromium 有 performance.memory）",
  /performance\.memory/.test(SAMPLER) && /不可用|不提供/.test(PANEL));

group("F 挂载与文档（假接线比没做更糟）");
ok("F1 面板真的挂在运维审计页（import + 标签）",
  /import MetricsPanel/.test(OPS) && /<MetricsPanel \/>/.test(OPS));
ok("F2 面板根节点是 ops-sec（沿用运维页同一套外壳，不另立版式）",
  /class="ops-sec metrics-sec"/.test(PANEL));
ok("F3 采样器 / 面板 / store 三个文件都在",
  existsSync(join(root, "src/utils/perfSampler.js")) && existsSync(join(root, "src/components/MetricsPanel.vue"))
  && existsSync(join(root, "src/stores/metrics.js")));
ok("F4 后端指标埋点清单存在（唯一埋点来源 = services/metrics.py）",
  existsSync(join(root, "..", "backend", "app", "services", "metrics.py")));

group("G 本钉已并入 verify 链");
ok("G1 package.json 的 verify 链里有 verify_metrics.mjs", /verify_metrics\.mjs/.test(PKG));

console.log(String.fromCharCode(10) + `合计：${pass} 过 / ${fail} 败`);
process.exit(fail ? 1 : 0);
