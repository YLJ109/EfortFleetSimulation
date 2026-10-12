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
// ★ 2026-10-11：工艺时序常量的唯一来源（吸牢/放气/检测位停留），缺省值钉在这里
const svc = read("../backend/app/services/sorting.py");

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

  // ★★ 实测抓到的漏洞：**真机路径**与**空跑路径**写的 stage 不是同一套！
  //   真机：`_set(st, stage=label)` → 气路标签 "吸"/"停止吸"/"放"/"停止放"（反面同理）；
  //   空跑：`stage="%s(空跑)" % stage` → 语义名 "取料·吸(空跑)"
  //   只映射语义名 → **真机跑起来流程条认不出、不跳步**（现场最常看的恰恰是真机）。
  //   这里直接从后端的标签表**解析**出全部字面量来断言，不手写（手写会漏）。
  // ★★ 2026-10-11（用户权威位表）：真机气路标签不再是 `_air` 里的 dict，而是收敛到
  //   `backend/app/services/modbus.py` 的 `VAC_ACTIONS`（8 个动作 → (位值, 中文标签)）。
  //   只认形如 `"suck": (VAC_BIT_SUCK, "吸")` 的**条目行**，避免把注释里的中文误当标签。
  const modbusPy = read("../backend/app/services/modbus.py");
  const vacBlock = (modbusPy.match(/VAC_ACTIONS[^\n]*=\s*\{([\s\S]*?)\n\}/) || [, ""])[1];
  const labels = [...vacBlock.matchAll(/"(\w+)":\s*\([^,]+,\s*"([^"]+)"\)/g)]
    .map((m) => m[2]).filter((v) => /[\u4e00-\u9fff]/.test(v));
  ok(`★ 后端气路标签解析到 ${labels.length} 个（下板 4 + 反面 4）`, labels.length >= 8,
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
    // ★★ 2026-10-11：双面（一趟两件）真机路径新增的**反面**气路标签（两面独立）
    ["反面吸", "suck"],
    ["反面停止吸", "stop_suck"],
    ["反面放", "blow"],
    ["反面停止放", "stop_blow"],
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
  // ★★ 2026-10-11：缺省值改从**工艺时序唯一来源** `services/sorting.py::SUCK_HOLD_MS` 取
  //   （值仍是 500ms）。口径仍钉死"默认 500"，只是不再各处硬写 500。
  ok("后端 StartIn 有 suck_hold_ms（缺省 = S.SUCK_HOLD_MS = 500ms）",
    /suck_hold_ms: int = Field\(default=S\.SUCK_HOLD_MS/.test(api)
    && /^SUCK_HOLD_MS = 500/m.test(svc));
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

  // 3) ★★ 2026-10-12 终版（用户口径「不用录制正反面」）：J6正面/反面已是**虚拟切面命令**
  //    （正面 = 该点自身录制的 J6、反面 = 当前点 J6 − 180，按点名自动折算，不读点位库）
  //    → 旧的"缺两行就拦下启动"护栏（dualFaceBlocked / DUAL_FACE_POINTS / dualFaceMissing）
  //    整体删除。反向钉住：不许再有按"库里有没有这两行"拦启动的守卫（否则用户删掉
  //    历史遗留行后，快速分拣会被 400 卡死 —— 界面守卫与后端口径脱节）。
  ok("★ 旧护栏 dualFaceBlocked / DUAL_FACE_POINTS / dualFaceMissing 已整体移除",
    !/dualFaceBlocked|DUAL_FACE_POINTS|dualFaceMissing/.test(view));
  ok("★ 快速分拣文案明确「原地翻转 + 无需录制正反面」（自动折算，不要求库里有这两行）",
    /原地翻转/.test(view) && /无需录制正反面/.test(view));
  ok("选快速时给了「首次使用请先空跑」的如实提示",
    /首次使用请先空跑/.test(view));
}

console.log("");
console.log("== G. J6 正面/反面：虚拟切面命令（用户 2026-10-12 终版「不用录制正反面」） ==");
{
  // ★★ 2026-10-12 终版：这两项不再读点位库 —— 正面 = 该点自身录制的 J6、
  //   反面 = 当前点 J6 − 180（自动折算）。界面绿点常亮（没有「未示教」态）、
  //   按钮固定叫「切面」；旧的 j6Only 标记 / j6Text 函数 / 存点按钮全部退役
  //   （虚拟命令没有"存的值"，列表里再列 6 个关节值就是在编故事）。
  ok("★ extraSlots 仍按名字列出 J6正面 / J6反面（与后端点名一字不差）",
    /\{\s*name:\s*"J6正面"/.test(view) && /\{\s*name:\s*"J6反面"/.test(view));
  ok("★ 旧 j6Only 标记已整体退役（不再有按标记分支的渲染）",
    !/j6Only/.test(view));
  ok("★ 旧 j6Text 取值函数已删除", !/j6Text/.test(view));
  ok("★ 界面如实标注「虚拟切面命令（无需录制）」", /虚拟切面命令/.test(view));
  // ★ 2026-10-11（方案 S2）：原来"只用 J6 / 其余轴不用 / J1~J5 保持实时姿态"在同一张卡讲了
  //   三遍。语义统一收敛到卡底**一段**说明 —— 断言随之收敛。
  ok("★ 卡底一段说明「只用 J6；翻转时 J1~J5 保持当前实时姿态」",
    /只用 J6/.test(view) && /当前实时姿态/.test(view));
  // 反证：普通点位仍走完整 6 值渲染（coordText 未被整体替换掉）。
  ok("★ A/B 主列表仍显示完整坐标（coordText 仍在）",
    /coordText\(ptOf\(sl\.name\)\)/.test(view));

  // ★★ 2026-10-12 终版卡内专属断言：先圈出 extraSlots 的模板块（从终版注释到 </template>），
  //   块内不许有「存点」（虚拟命令无需录制）也不许有「到点」（那是整姿态动作，这里是原地切面）。
  const iJ6 = view.indexOf("终版：J6正面/反面");
  const extraBlock = iJ6 >= 0 ? view.slice(iJ6, view.indexOf("</template>", iJ6)) : "";
  ok("★ 卡内绿点常亮（虚拟命令没有「未示教」态，sdot on 静态渲染）",
    extraBlock.length > 0 && extraBlock.includes("sdot on"));
  ok("★ 卡内按钮是「切面」（原地只改 J6，不是整姿态「到点」）",
    /切面\s*<\/button>/.test(extraBlock) && !/到点/.test(extraBlock));
  ok("★ 卡内没有存点按钮（teach —— 虚拟命令无需录制）",
    extraBlock.length > 0 && !/teach\(/.test(extraBlock));

  // ★★ 点位执行页：`J6正面`/`J6反面` 同样**只显示 J6**（后端已把下发转成"原地切面"），
  //   不把"其余 5 轴写 0"当成坐标列出来（否则界面像在说这 5 轴也参与）。
  const pv = read("src/components/PointExecView.vue");
  ok("★ 点位执行页对 J6 正/反面只显示 J6（isJ6Face 分支 + 「仅 J6 有效」标注）",
    /isJ6Face\(p\)/.test(pv) && /仅 J6 有效/.test(pv));
  ok("★ 点位执行页这两点的执行按钮标成「切面」",
    /isJ6Face\(p\) \? "切面" : "执行"/.test(pv));
}

console.log("");
console.log("== H. 「当前面」如实显示（取自后端真实字段 state.face，标注为流程推断） ==");
{
  // ★★ 2026-10-11（方案 §A）用户要求：分拣页要**如实显示当前面（正面/反面）**。
  //   三条铁律（本项目口径）：① 数据必须取自**后端真实字段** state.face（由后端 `_move` 每次如实写）；
  //   ② 现场**没有"面"传感器** → 必须标注为"流程推断"；③ 取不到 → 显示「—/未知」，**绝不编**。
  ok("★ 当前面数据取自后端 state.face（不另算、不写死）",
    /st\.value\s*&&\s*st\.value\.face/.test(view), "找不到读取 state.face 的地方");
  ok("★ 如实映射：front→正面 / back→反面",
    /f === "front" \? "正面" : f === "back" \? "反面"/.test(view));
  ok("★ 标注为「流程推断」（现场无面传感器）",
    /流程推断/.test(view) && /无面传感器/.test(view));
  ok("★ 模板里有「当前面」一行且取不到时显示「—（未知）」",
    /当前面/.test(view) && /v-if="faceNow"/.test(view) && /（未知）/.test(view));
  // 反证：判据不是恒真（随便编一个函数名不应命中）
  ok("反证：不存在的字段读取判据不会被误命中", !/st\.value\.not_a_real_field/.test(view));

  // ★★ 2026-10-11（本轮 C）固定顺序：拍照**先反面后正面**、放料**先正面后反面**。
  //   顺序在**后端**（唯一口径函数 `faces_for_capture` / `order_for_drop`）；这里钉住后端确实走了它们，
  //   而不是在流程里"另排一遍"（那正是最容易排乱、且编译不报的地方）。
  ok("★ 后端送检拍照面序走唯一口径 faces_for_capture（先反面后正面）",
    /faces_for_capture/.test(api), "送检拍照面序没走唯一口径");
  ok("★ 后端落料次序走唯一口径 order_for_drop（先正面后反面）",
    /order_for_drop/.test(api), "落料次序没走唯一口径");
  // 反证：旧的"先有效后无效"表达式不应再出现在双面流程里
  ok("反证：旧的「先有效件再无效件」排序表达式已从双面流程移除",
    !/\[v for v in verdicts if v\.get\("ok"\)\]\s*\+/.test(api));
}

console.log("");
console.log("结果: " + pass + " 通过 / " + fail + " 失败");
process.exit(fail ? 1 : 0);
