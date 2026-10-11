// =====================================================================
// 执行流程可视化（SortFlow.vue）回归 —— headless 源码/语义扫描
//
//   为什么需要它：流程条的价值全在"**和后端真实执行一致**"。
//   一旦后端改了 stage 文案、而流程表的映射没跟着改，界面会静默变成
//   "永远停在第 1 步"或"高亮跳错步" —— 编译不报错、看着也像个正常界面，
//   现场却拿它当"机器人走到哪"的依据 → 比没有还糟。
//
//   钉三类：
//   A. 覆盖性：后端 sorting.py 里出现的**每一个** stage 字面量，
//      STAGE_MAP 都必须能把它映射到某一步（否则就是"认不出来"）。
//   B. 语义：步骤顺序必须与真实工艺一致（先取料后送检、先停止吸再放气、
//      放气就地停 → 回起点），顺序错了流程条就是错的教科书。
//   C. 抓帧展示：必须**如实列出全部帧**并只把置信度最高的一张标为「主」，
//      且判色失败时也要指出"最像的那一张"。
//
// 运行： cd frontend && node tools/verify_sortflow.mjs
// =====================================================================
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { stepOf, LOOP_STEPS, PRE_STEP, POST_STEP, STEP_ORDER, STAGE_MAP }
  from "../src/sorting/flowSteps.js";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (rel) => readFileSync(join(root, rel), "utf8");

let pass = 0, fail = 0;
function ok(name, cond, extra = "") {
  if (cond) { pass++; console.log("  PASS  " + name); }
  else { fail++; console.log("  FAIL  " + name + (extra ? "   → " + extra : "")); }
}

const flow = read("src/components/SortFlow.vue");
const view = read("src/components/SortingView.vue");
const api = read("../backend/app/api/sorting.py");

// 去掉注释行再扫描（注释里会写"以前是 xxx"来解释历史，不该被当成还在用）
const strip = (src) => src.split("\n").filter((l) => !l.trim().startsWith("//")
  && !l.trim().startsWith("#") && !l.trim().startsWith("*")).join("\n");

console.log("== A. 后端 stage 覆盖率（每个 stage 都要能落到某一步） ==");
{
  // 从 sorting.py 里抓出所有 stage 字面量：_set(..., stage="X") / _move(..., "X") / _air(..., "X")
  const setStages = [...api.matchAll(/stage="([^"%]+)"/g)].map((m) => m[1]);
  const moveStages = [...api.matchAll(/_move\(ctx, pmap, [^)]*?, "(取料·[^"]+|抬起|送检|放料·[^"]+|回[^"]+|收尾[^"]+|异常[^"]+)"\)/g)]
    .map((m) => m[1]);
  const airStages = [...api.matchAll(/_air\(ctx, "\w+", \w+, "(取料·[^"]+|放料·[^"]+)"\)/g)]
    .map((m) => m[1]);
  const all = [...new Set([...setStages, ...moveStages, ...airStages])];

  // 流程表里的 stage 模式 + STAGE_MAP 的键
  // ★ 直接读**模块数据**（flowSteps.js），不扫组件源码：步骤表已抽成纯模块，
  //   扫源码会变成"改个写法就失效"的脆弱断言；读数据则改哪儿都跑不掉。
  const pats = [PRE_STEP, ...LOOP_STEPS, POST_STEP].flatMap((s) => s.stages);
  const mapKeys = Object.keys(STAGE_MAP).filter(Boolean);

  const known = new Set([...pats, ...mapKeys]);
  const missing = all.filter((s) => !known.has(s)
    // 允许子串匹配兜底（如 "取料·吸(空跑)" → 前缀 "取料·吸"）
    && !pats.some((p) => s.startsWith(p)));
  ok(`后端出现的 ${all.length} 个 stage 全部有映射`, missing.length === 0,
    "未覆盖: " + JSON.stringify(missing));
  ok("确实是抓到了 stage（判据不是恒真）", all.length >= 12, "抓到 " + all.length);

  // ★★ 2026-10-09 实测抓到的漏洞：**真机路径**与**空跑路径**写的 stage 不是同一套！
  //   真机：`_set(st, stage=label)` → 气路标签 "吸"/"停止吸"/"放"/"停止放"
  //   空跑：`stage="%s(空跑)" % stage` → 语义名 "取料·吸(空跑)"
  //   只映射语义名 → **真机跑起来流程条认不出、不跳步**（现场最常看的恰恰是真机）。
  //   这里直接从后端的 label 字典**解析**出这四个字面量来断言，不手写（手写会漏）。
  //   注意：字典里键是英文动作名（suck/release/blow/unblow），只有**值**才是阶段文案 ——
  //   所以要按"含中文"过滤，否则会把 suck/blow 这些键也当成待映射的 stage（第一版就错了）。
  const labelDict = (api.match(/label = \{([^}]+)\}\.get\(action, action\)/) || [, ""])[1];
  const labels = [...labelDict.matchAll(/"([^"]+)"/g)].map((m) => m[1])
    .filter((v) => /[\u4e00-\u9fff]/.test(v));
  ok(`★ 后端气路标签解析到 ${labels.length} 个（吸/停止吸/放/停止放）`, labels.length >= 4,
    JSON.stringify(labels));
  const labelMissing = labels.filter((l) => !mapKeys.includes(l));
  ok("★★ 真机路径的气路标签全部有映射（否则真机时流程条不跳步）",
    labelMissing.length === 0, "未覆盖: " + JSON.stringify(labelMissing));

  // 反证：编一个后端不存在的 step 名，必须被判为"未覆盖"
  const fake = all.concat(["凭空捏造的阶段"]);
  ok("反证：不存在的 stage 会被判未覆盖",
    fake.some((s) => !known.has(s) && !pats.some((p) => s.startsWith(p))));
}

console.log("");
console.log("== B. 步骤顺序与真实工艺一致 ==");
{
  // ★ 直接断言**模块里的实际顺序**（不是扫源码里的出现位置）：
  //   顺序错了流程条就是错的教科书 —— 它会教操作员"先放气再停止吸"。
  const order = LOOP_STEPS.map((s) => s.key);
  const WANT = ["a_up", "a_down", "suck", "lift", "to_cam", "dwell", "capture",
                "b_up", "stop_suck", "blow", "stop_blow", "back"];
  ok("12 个循环步骤齐备", order.length === 12);
  ok("定义顺序 = 工艺顺序（先取料 → 送检 → 判色 → 放料 → 回起点）",
    JSON.stringify(order) === JSON.stringify(WANT),
    "实际: " + JSON.stringify(order));

  const pos = (k) => order.indexOf(k);
  ok("★ 先停止吸、再放气（stop_suck 在 blow 之前）", pos("stop_suck") < pos("blow"));
  ok("★ 放气就地停（stop_blow 在 back 之前）", pos("stop_blow") < pos("back"));
  ok("★ 判色在放料之前（capture 在 b_up 之前）", pos("capture") < pos("b_up"));
  ok("★ 送检在取料之后（to_cam 在 suck 之后）", pos("to_cam") > pos("suck"));
  ok("★ 每个步骤都有 label（否则界面会渲染空白行）",
    [PRE_STEP, ...LOOP_STEPS, POST_STEP].every((s) => typeof s.label === "function"
      || (typeof s.label === "string" && s.label.length > 0)));
  ok("★ 每个步骤都声明了 stages（否则它永远不会被高亮）",
    [PRE_STEP, ...LOOP_STEPS, POST_STEP].every((s) => Array.isArray(s.stages)
      && s.stages.length > 0));
}

console.log("");
console.log("== B2. 用**实测抓到的真实 stage 序列**驱动映射（这是最硬的一条） ==");
{
  // ★ 2026-10-09 真机实测：后端跑起来 stage 依次出现下面这些（从 /sorting/state 轮询实录）。
  //   注意气路那几步是真机标签（吸/停止吸/放/停止放），与空跑写法不同。
  const REAL = [
    ["回开始/结束点", "start"],
    ["取料", "a_up"],              // 每件开头的通用名，归到取料第一步
    ["取料·上方", "a_up"],
    ["取料·下落", "a_down"],
    ["吸", "suck"],                // ★ 真机气路标签
    ["抬起", "lift"],
    ["送检", "to_cam"],
    ["停顿", "dwell"],
    ["判色", "capture"],
    ["放料·正上方", "b_up"],
    ["停止吸", "stop_suck"],       // ★ 真机气路标签
    ["放", "blow"],                // ★ 真机气路标签
    ["停止放", "stop_blow"],       // ★ 真机气路标签
    ["回起点", "back"],
    ["收尾·回开始/结束点", "end"],
  ];
  const wrong = REAL.filter(([stage, want]) => stepOf(stage) !== want);
  ok(`★ 真机实测 ${REAL.length} 个 stage 逐步映射全部正确`, wrong.length === 0,
    "错: " + JSON.stringify(wrong));
  ok("★ 反证：判据不是恒真（把 stage 换成乱码必须映射为空）", stepOf("不存在的阶段") === "");

  // 空跑序列（新代码保留语义名 + (空跑) 后缀）——同一个步骤，写法不同
  const DRY = [
    ["取料·吸(空跑)", "suck"],
    ["放料·停止吸(空跑)", "stop_suck"],
    ["放料·放气(空跑)", "blow"],
    ["放料·停止放气(空跑)", "stop_blow"],
  ];
  const dryWrong = DRY.filter(([stage, want]) => stepOf(stage) !== want);
  ok("★ 空跑的带后缀写法（语义名+(空跑)）同样映射正确", dryWrong.length === 0,
    "错: " + JSON.stringify(dryWrong));

  // 前缀兜底不许被短标签抢走：语义名必须完整命中
  ok("★ 前缀兜底按长度降序（「取料·吸」不会被「吸」抢走）",
    stepOf("取料·吸(空跑)") === "suck" && stepOf("放料·停止吸(空跑)") === "stop_suck");

  // 每一步都可达：真实序列覆盖了循环体的每一步（有步骤永远走不到 = 流程条有假步骤）
  const reached = new Set(REAL.map(([, k]) => k));
  const unreachable = LOOP_STEPS.map((s) => s.key).filter((k) => !reached.has(k));
  ok("★ for 循环体每一步都能被真实 stage 触达（没有走不到的空步骤）",
    unreachable.length === 0, "走不到: " + JSON.stringify(unreachable));
  ok("前后两步也在序列里", reached.has(PRE_STEP.key) && reached.has(POST_STEP.key));
  ok("步骤顺序表覆盖全部 key（高亮判定不会漏）",
    STEP_ORDER.length === LOOP_STEPS.length + 2
    && STEP_ORDER.every((k) => k === PRE_STEP.key || k === POST_STEP.key
      || LOOP_STEPS.some((s) => s.key === k)));
  ok("STAGE_MAP 里没有指向未知步骤的键",
    Object.values(STAGE_MAP).every((v) => v === "" || STEP_ORDER.includes(v)),
    JSON.stringify(Object.entries(STAGE_MAP).filter(([, v]) => v && !STEP_ORDER.includes(v))));
}

console.log("");
console.log("== C. 抓帧如实展示（用户：捕捉 3 张，只显示一张置信度最高的） ==");
{
  ok("列出**全部**帧（v-for 遍历 frameList，不是只取一张）",
    /v-for="\(f, i\) in frameList"/.test(flow));
  ok("★ 置信度最高的一张单独标记为「主」", /i === bestIdx/.test(flow) && /主/.test(flow));
  ok("后端给了 best_frame 就用它（与后端口径一致，不自己另算一套）",
    /s\.value\.best_frame/.test(flow));
  ok("★ 判色失败时也要指出「最像的那一张」（否则现场只看到「检测不到」）",
    /判色失败时也要指出/.test(flow) && /c > bc/.test(flow));
  ok("帧上带颜色色块 + 置信度数值",
    /class="swatch"/.test(flow) && /toFixed\(2\)/.test(flow));
  ok("未判出的帧有区别于正常帧的样式（bad）", /f\.ok/.test(flow) && /\.frame\.bad/.test(flow));
}

console.log("");
console.log("== D. for 循环形态 + 动画高亮（用户：「for 循环形式那种…动画高亮形式做一个盒子」） ==");
{
  ok("★ 循环体用竖线+缩进圈起来，并标注 for i = 1..N",
    /class="loop"/.test(flow) && /for 第/.test(flow) && /i = 1 …/.test(flow));
  ok("★ 当前步有动画高亮（@keyframes + .step.on）",
    /@keyframes pulse/.test(flow) && /\.step\.on/.test(flow) && /animation: pulse/.test(flow));
  ok("已走过的步骤有区分（done）", /\.step\.done/.test(flow) && /stepState/.test(flow));
  ok("循环外也有开始/结束两步（不是只有循环体）",
    /PRE_STEP/.test(flow) && /POST_STEP/.test(flow));
}

console.log("");
console.log("== E. 接进分拣页 + 参数不写死 ==");
{
  ok("分拣页引入了 SortFlow 并传 state", /<SortFlow/.test(view) && /:st="st"/.test(view));
  ok("★ 参数取本轮真实值（不是把 500/1000 写死在文案里）",
    /:dwell-ms="dwellMs"/.test(view) && /:blow-ms="blowMs"/.test(view)
    && /:suck-hold-ms="suckHoldMs"/.test(view) && /:frames="frames"/.test(view));
  ok("前端把 suck_hold_ms 一起下发给后端（界面与后端同一份参数）",
    /suck_hold_ms: suckHoldMs\.value/.test(view));
  ok("后端 StartIn 有 suck_hold_ms（默认 500）",
    /suck_hold_ms: int = Field\(default=500/.test(api));
  ok("★ 后端空跑分支保留语义化阶段名（否则空跑时流程高亮跳错步）",
    /stage=\("%s\(空跑\)" % stage\)/.test(api));
}

console.log("");
console.log("== F. 分拣模式（正常 / 快速）→ 后端 dual_face ==");
{
  // ★ 为什么钉这三条：这个控件直接决定**真机动作**是"单面一趟一件"还是"双面一趟两件"。
  //   一旦默认值被改成「快速」、或请求体把 dual_face 写死、或护栏被删掉，
  //   编译/界面都看不出异常，但现场会拿错流程开跑（多一次 J6 翻转 / 跑到翻转找不到点）。
  //   所以全部按**源码事实**断言，不允许"看起来像"。
  //
  // 1) 默认模式 = 正常分拣（且没有持久化 —— 每次进页面归零）。
  ok("★ 默认模式 = 正常分拣（mode 初始 normal）",
    /const mode = ref\("normal"\)/.test(view), "找不到 mode 默认值");
  ok("正常分拣对应 dual_face=false（dualFace 仅 fast 时为真）",
    /dualFace\s*=\s*computed\(\(\)\s*=>\s*mode\.value === "fast"\)/.test(view));

  // 2) 请求体里 dual_face 由模式决定，不能写死。
  ok("★ 请求体 dual_face 由模式决定（dual_face: dualFace.value）",
    /dual_face:\s*dualFace\.value/.test(view));
  ok("反证：把 dual_face 写死 true/false 会被判失败（当前没有写死）",
    !/dual_face:\s*(true|false)\b/.test(view));

  // 3) 缺 J6正面 / J6反面 时拦下启动（且沿用本页既有 isTaught，不另写查询）。
  ok("★ 有拦截逻辑：canStart 含 dualFaceBlocked",
    /canStart\b[\s\S]{0,240}dualFaceBlocked\.value/.test(view));
  ok("★ 拦截用到两个点名 J6正面 / J6反面",
    /DUAL_FACE_POINTS\s*=\s*\[\s*"J6正面"\s*,\s*"J6反面"\s*\]/.test(view));
  ok("★ 拦截沿用既有 isTaught 判定点位是否已示教（不新造查询）",
    /dualFaceMissing[\s\S]{0,120}isTaught\(/.test(view));
  ok("选快速时给了「首次使用请先空跑」的如实提示",
    /首次使用请先空跑/.test(view));
}

console.log("");
console.log("== G. J6 正面/反面：界面只显示 J6（用户 2026-10-11 反馈） ==");
{
  // ★ 用户口径："J6 的坐标不对，你应该只存储 J6 的坐标，别的五个点不用"。
  //   后端已把翻转改成**只覆盖第 6 轴**（`_move_flip_j6`）；界面这两行也必须**只报 J6**，
  //   否则列表里还列着 6 个关节值 → 会让人误以为那 5 个轴也参与翻转（界面在编故事）。
  //   做法：给这两项打 `j6Only: true`，模板按标记分支渲染（**不**用 sl.name === ... 散落硬编码）。
  const j6Lines = view.split("\n")
    .filter((l) => !l.trim().startsWith("//") && /j6Only:\s*true/.test(l));
  ok("★ j6Only 标记只打在 J6 两项上（C检测 / 开始/结束点 未被影响）",
    j6Lines.length === 2 && j6Lines.every((l) => /"J6/.test(l)),
    "打到: " + JSON.stringify(j6Lines.map((l) => l.trim())));
  ok("★ 坐标区有 j6-only 的分支渲染（v-if=\"sl.j6Only\"）",
    /v-if="sl\.j6Only"/.test(view));
  ok("★ 有 J6-only 取值函数（j6Text，只取 joints[5]）",
    /function j6Text\(/.test(view) && /j\[5\]/.test(view));
  ok("★ 明确标注「仅 J6 有效，其余轴不使用」",
    /仅 J6 有效，其余轴不使用/.test(view));
  // 反证：非 j6-only 的点仍走完整 6 值渲染（coordText 未被整体替换掉）。
  ok("★ 非 j6-only 行仍显示完整坐标（coordText 仍在）",
    /coordText\(ptOf\(sl\.name\)\)/.test(view));
}

console.log("");
console.log("结果: " + pass + " 通过 / " + fail + " 失败");
process.exit(fail ? 1 : 0);
