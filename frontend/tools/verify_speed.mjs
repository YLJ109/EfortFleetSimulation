// =====================================================================
// 速度口径守卫（headless 静态检查）
//
// 用户口径（2026-10-09，原话）：
//   "速度有问题，变成硬编码，默认速度5调一下，
//    去看看机器人速度**必须按右上角的速度执行**！！！"
//
// 全项目的速度只有**一份**：顶栏滑块 → `exec.speed` → 各接口的 `speed_pct`。
// 这个脚本把"三处必须一致、一处不许硬编码"钉死：
//   A. 顶栏滑块的下限 == 后端 `SPEED_MIN`（曾出现 UI min=1 / 后端 ge=5 不一致，
//      表现是"我设了 1% 却按 5% 跑"）；
//   B. `exec.speed` 的默认值是"推荐档"，**不能等于工程下限**（否则像被写死）；
//   C. **所有下发点都引用同一个变量**，不许出现字面量 `speed_pct: 5` 这种硬编码。
//
// 运行： cd frontend && node tools/verify_speed.mjs
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

// 从后端**单一来源**解析速度边界（手写数字一定会漂）
const SC = read("../backend/app/core/safety_const.py");
const mMin = SC.match(/^SPEED_MIN\s*=\s*(\d+)/m);
const mMax = SC.match(/^SPEED_MAX\s*=\s*(\d+)/m);
const SPEED_MIN = mMin ? Number(mMin[1]) : NaN;
const SPEED_MAX = mMax ? Number(mMax[1]) : NaN;
ok("从后端解析到速度边界（SPEED_MIN/MAX）",
   Number.isFinite(SPEED_MIN) && Number.isFinite(SPEED_MAX),
   `${SPEED_MIN}~${SPEED_MAX}`);

const app = read("src/App.vue");
const exec = read("src/stores/exec.js");
const ai = read("src/stores/ai.js");

console.log("");
console.log("== A. 顶栏滑块与后端边界一致 ==");
{
  const range = app.match(/<input[^>]*type="range"[^>]*class="speed-slider"[^>]*>/);
  const tag = range ? range[0] : "";
  ok("找到顶栏速度滑块", !!tag, tag.slice(0, 80));
  // ★ 2026-10-09：滑块现在**直接引用** core/safetyConst.js 的常量（不是字面量）——
  //   比"两处数字相等"更强：根本没有第二个数字可以漂。
  ok("★ 滑块 min/max 引用 core/safetyConst.js 的常量（不是字面量）",
     /:min="SPEED_MIN"/.test(tag) && /:max="SPEED_MAX"/.test(tag),
     tag.slice(0, 90));
  ok("★ App.vue 确实导入了这两个常量（防「用了没导入」）",
     /import \{[^}]*SPEED_MIN[^}]*\} from "\.\/core\/safetyConst\.js"/.test(app)
     || /from "\.\/core\/safetyConst\.js"/.test(app));
  // 前端常量必须与后端一致（两个文件，一个口径）
  const FE = read("src/core/safetyConst.js");
  const feMin = FE.match(/export const SPEED_MIN\s*=\s*(\d+)/);
  const feMax = FE.match(/export const SPEED_MAX\s*=\s*(\d+)/);
  ok(`★ 前端 safetyConst 与后端 safety_const 同值（${SPEED_MIN}~${SPEED_MAX}）`,
     !!feMin && !!feMax && Number(feMin[1]) === SPEED_MIN && Number(feMax[1]) === SPEED_MAX,
     feMin ? `${feMin[1]}~${feMax[1]}` : "未解析到");
  ok("滑块确实绑到全局 exec.speed（不是本地副本）",
     /v-model\.number="exec\.speed"/.test(tag));
}

console.log("");
console.log("== B. exec.speed 的默认值与夹取范围 ==");
{
  // ★★ 2026-10-11 用户口径（以这次为准）："**每次启动默认 10%**，下限仍是 5%"。
  //   —— 默认值来自唯一来源 SPEED_DEFAULT（core/safetyConst.js），**不再等于工程下限**。
  //   注意：5%→10% 属**真机提速一倍**，是用户明确要求。
  const usesStorage = /localStorage\.getItem\("exec\.speed"\)/.test(exec)
                   || /localStorage\.setItem\("exec\.speed"/.test(exec);
  ok("★ 不再读写 localStorage（否则又会\"记住上次的值\"）", !usesStorage);
  const FE = read("src/core/safetyConst.js");
  const feDefault = FE.match(/export const SPEED_DEFAULT\s*=\s*(\d+)/);
  ok("★ safetyConst 定义了 SPEED_DEFAULT（默认档的唯一来源）", !!feDefault,
     feDefault ? feDefault[1] : "未找到");
  const DEF = feDefault ? Number(feDefault[1]) : NaN;
  ok(`★ 启动默认值 == SPEED_DEFAULT(${DEF})（用户明确要求：每次启动 10%）`,
     /return SPEED_DEFAULT;/.test(exec) || new RegExp(`return ${DEF};`).test(exec),
     "未找到默认返回");
  ok(`★ 默认值(${DEF}) > 工程下限 SPEED_MIN(${SPEED_MIN}) —— 默认档 ≠ 下限（否则像被写死）`,
     Number.isFinite(DEF) && DEF > SPEED_MIN, `${DEF} vs ${SPEED_MIN}`);
  // ★ 用户口径的硬数字：下限 = 5、启动默认 = 10（写死这两个数，防漂移）
  ok("★ 工程下限 SPEED_MIN === 5", SPEED_MIN === 5, String(SPEED_MIN));
  ok("★ 启动默认 SPEED_DEFAULT === 10", DEF === 10, String(DEF));
  ok("旧的宽松校验（`v >= 1`）已不存在",
     !/v\s*>=\s*1\s*&&\s*v\s*<=\s*100/.test(exec));
}

console.log("");
console.log("== C. 所有下发点都引用同一份速度（无硬编码） ==");
{
  const hard = [...exec.matchAll(/speed_pct:\s*(\d+)/g)].map((x) => x[1])
    .concat([...ai.matchAll(/speed_pct:\s*(\d+)/g)].map((x) => x[1]));
  ok("★ 前端没有硬编码的 speed_pct 数字", hard.length === 0, JSON.stringify(hard));

  // 各 store 的引用形态：this.speed（exec 内部）/ exec.speed（跨 store）
  ok("exec store 内部下发用 `this.speed`", /speed_pct:\s*this\.speed/.test(exec));
  ok("AI store 下发用 `exec.speed`（跨 store 引用全局那一份）",
     /speed_pct:\s*exec\.speed/.test(ai));

  // 分拣页也必须是全局那一份（它最容易被人另起一份）
  const sortView = read("src/components/SortingView.vue");
  ok("★ 分拣页下发用 `exec.speed`（不许自己另起一份）",
     /speed_pct:\s*exec\.speed/.test(sortView));

  // 后端：接口的默认值只作兜底，且必须落在 [SPEED_MIN, SPEED_MAX]
  const api = read("../backend/app/api/sorting.py");
  const d = api.match(/speed_pct:\s*int\s*=\s*Field\(default=(\d+)/);
  ok(`后端分拣接口的默认值在 [${SPEED_MIN}, ${SPEED_MAX}] 内`,
     d && Number(d[1]) >= SPEED_MIN && Number(d[1]) <= SPEED_MAX,
     d ? d[1] : "未找到");
}

console.log("");
console.log("== D. 反证（判据不是恒真的） ==");
{
  // 造一个"硬编码"样本，必须能被 C 段的判据抓到
  const fake = 'body: JSON.stringify({ speed_pct: 5 })';
  ok("反证：硬编码样本会被判据抓到", /speed_pct:\s*(\d+)/.test(fake));
  // 造一个"下限不一致"样本
  const fakeTag = '<input type="range" min="1" max="100" class="speed-slider" />';
  ok("反证：min=1 的旧滑块会被 A 段判为不一致",
     !/:min="SPEED_MIN"/.test(fakeTag));
}

console.log("");
console.log("结果: " + pass + " 通过 / " + fail + " 失败");
process.exit(fail ? 1 : 0);
