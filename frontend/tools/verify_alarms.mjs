// =====================================================================
// C2「报警合理化」守卫（headless 静态检查）
//
// 本轮做的是**显示与处置层**（EEMUA-191 / ISA-18.2 的最小可用版），它最容易出的
// 事故不是「功能没做」，而是**做着做着变成了第二套安全判据**：
//   ① 报警能被搁置/确认 → 就有人（包括代码自己）把它当成「互锁也能被静音」；
//   ② 延时确认（on-delay）套到 danger/hit/急停上 → 危险信号晚到 = 没到；
//   ③ 「出现太多次就降级」→ 严重度被静默改掉，操作员看到的颜色骗人；
//   ④ 前端用列表长度当刷新水位 → 折叠/清除让长度变小，新报警再也看不见（§5.5 铁律①）；
//   ⑤ 没登记的报警被吞掉 / 未知塌成 low → 「一切正常」的假安静（铁律③）。
// 所以这里既钉后端判据与红线，也钉 REST 口径、审计出口、界面诚实性与「刹车不受影响」。
//
// ★★ 反证的核心一条在 pytest 里（真跑函数比返回值）：
//    tests/test_alarms.py::test_shelving_never_affects_guard_check
//    本脚本只能钉「这条测试确实存在且没被删」，钉不了行为 —— 行为去跑 pytest。
//
// 运行： cd frontend && node tools/verify_alarms.mjs
// =====================================================================
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (rel) => readFileSync(join(root, rel), "utf8").split(String.fromCharCode(13, 10)).join(String.fromCharCode(10));

let pass = 0, fail = 0;
function ok(name, cond, extra = "") {
  if (cond) { pass++; console.log("  PASS  " + name); }
  else { fail++; console.log("  FAIL  " + name + (extra ? "   → " + extra : "")); }
}

/** 剥块注释 / HTML 注释 / 整行 //（注释里写「不导入 motion」不该把红线判红）。 */
function stripComments(s) {
  return s.replace(/\/\*[\s\S]*?\*\//g, "")
          .replace(/<!--[\s\S]*?-->/g, "")
          .replace(/^\s*\/\/.*$/gm, "");
}

/** Python：只在字符串外遇到 # 才砍到行尾（判「真有没有调用」，不能被注释里提一句吓住）。 */
function stripPy(s) {
  let out = "", q = null, i = 0;
  while (i < s.length) {
    const c = s[i];
    if (q) {
      out += c;
      if (c === "\\") { out += s[i + 1] || ""; i += 2; continue; }
      if (s.startsWith(q, i)) { out += q; i += q.length; q = null; continue; }
      i++; continue;
    }
    if (c === "#") { while (i < s.length && s[i] !== "\n") i++; continue; }
    if (c === '"' || c === "'") {
      q = s.startsWith(c.repeat(3), i) ? c.repeat(3) : c;
      out += q; i += q.length; continue;
    }
    out += c; i++;
  }
  return out;
}

const al = read("../backend/app/services/alarms.py");
const alCode = stripPy(al);
const apiAl = read("../backend/app/api/alarms.py");
const apiAlCode = stripPy(apiAl);
const evs = read("../backend/app/services/events.py");
const evsCode = stripPy(evs);
const mainPy = read("../backend/app/main.py");
const guardPy = read("../backend/app/services/safety_guard.py");
const evalPy = read("../backend/app/services/safety_eval.py");
const motionPy = read("../backend/app/services/motion.py");
const modbusPy = read("../backend/app/services/modbus.py");
const jogPy = read("../backend/app/services/jog.py");
const rcPy = read("../backend/app/services/rc_ready.py");
const tests = read("../backend/tests/test_alarms.py");
const comp = read("src/components/EventsView.vue");
const compCode = stripComments(comp);
const execJs = stripComments(read("src/stores/exec.js"));
const pkg = read("package.json");

// ---------------------------------------------------------------------
console.log("");
console.log("== A. 报警字典：唯一登记口 + 登记时就地拒绝 ==");
{
  ok("A1 登记表只有两处写点：register()（动作名型，逐条过红线）与 _register_code()（报警码型，"
     + "一律 safety=True 且不登记延时/自动清除）；_BY_ACTION 只在 register() 里写",
     (al.match(/^[ \t]*_RULES\[.+\] = .*/gm) || []).length === 2 &&
     (al.match(/^[ \t]*_BY_ACTION\[.+\] = .*/gm) || []).length === 1 &&
     /_RULES\[rule\.id\] = rule/.test(al) &&
     /_RULES\[r\.id\] = r/.test(al) &&
     /def _register_code\([\s\S]{0,520}Rule\(id="rc\.alarm\.%d"[\s\S]{0,300}?safety=True\)/.test(al) &&
     !/def _register_code\([\s\S]{0,520}(delay_ms|auto_resolve_ms)/.test(al));
  ok("A2 每条登记都带 priority/consequence/action/source 四要素（界面靠它说清「要我做什么」）",
     /id: str[\s\S]{0,400}priority: str[\s\S]{0,400}consequence: str[\s\S]{0,200}action: str[\s\S]{0,200}source: str/.test(al) &&
     /if not \(rule\.consequence and rule\.action\):/.test(al));
  ok("A3 优先级只有四档，且必须显式给（不是随手写字符串）",
     /PRIORITIES = \(P_CRITICAL, P_HIGH, P_MEDIUM, P_LOW\)/.test(al) &&
     /if rule\.priority not in PRIORITIES:\n\s+raise ValueError/.test(al));
  ok("A4 ★ 红线①：安全类报警带延时 → register() 直接拒绝（不是运行时才降级）",
     /if rule\.safety and rule\.delay_ms > 0:\n\s+raise ValueError\("红线：安全类报警禁止延时确认/.test(al));
  ok("A5 ★ 红线②：安全类不许自动清除；critical/high 必须人确认",
     /if rule\.safety and rule\.auto_resolve_ms > 0:\n\s+raise ValueError\("红线：安全类报警禁止自动清除/.test(al) &&
     /if rule\.priority in \(P_CRITICAL, P_HIGH\) and rule\.auto_resolve_ms > 0:/.test(al));
  ok("A6 字典覆盖：≥25 条动作型登记 + 4 个控制器报警码（5005/3909/1812/4902）",
     (al.match(/_R\(Rule\(/g) || []).length >= 25 &&
     (al.match(/_register_code\(/g) || []).length >= 5 &&
     /_register_code\(5005,/.test(al) && /_register_code\(3909,/.test(al) &&
     /_register_code\(1812,/.test(al) && /_register_code\(4902,/.test(al));
  ok("A7 急停 / 围栏 danger·hit / 互锁缺失 / 下发被拦 全部登记成安全类（safety=True）",
     /Rule\("control\.estop",[\s\S]{0,300}?safety=True\)/.test(al) &&
     /Rule\("control\.estop_failed",[\s\S]{0,300}?safety=True\)/.test(al) &&
     /Rule\("control\.interlock_absent",[\s\S]{0,300}?safety=True\)/.test(al) &&
     /Rule\("control\.move_blocked",[\s\S]{0,300}?safety=True\)/.test(al) &&
     /for _st, _pr in \(\("hit", P_CRITICAL\), \("danger", P_HIGH\), \("warn", P_MEDIUM\)\)[\s\S]{0,600}?safety=True/.test(al));
  ok("A8 未登记的报警**不吞**：如实标 unclassified 并给出「请登记这条」的动作",
     /P_UNCLASSIFIED = "unclassified"/.test(al) &&
     /def unclassified_rule\(/.test(al) &&
     /仍然显示|仍照常显示/.test(al) &&
     /未在报警字典登记/.test(al));
  ok("A9 ★ 未分级不垫底：排在 high 之后、medium 之前（不知道多严重不能让人先看不痛不痒的）",
     /_RANK = \{P_CRITICAL: 0, P_HIGH: 1, P_UNCLASSIFIED: 2, P_MEDIUM: 3, P_LOW: 4\}/.test(al) &&
     /def priority_rank\(priority: str\)/.test(al));
  ok("A10 ★ 优先级唯一的可写点是 set_priority（全文件 .priority 只被赋值一次），改级必须写理由",
     (al.match(/\.priority = /g) || []).length === 1 &&
     /def set_priority\([\s\S]{0,900}rec\.priority = priority/.test(al) &&
     /if not \(reason or ""\)\.strip\(\):\n\s+raise AlarmOpError\("改优先级必须写理由/.test(al));
  ok("A11 重复出现只加 count，绝不动 priority（铁律⑥的源码侧保证）",
     /优先级\*\*只\*\*来自字典或操作员显式动作/.test(al) &&
     !/priority = (P_LOW|P_MEDIUM|P_HIGH|P_CRITICAL)/.test(alCode));
}

// ---------------------------------------------------------------------
console.log("");
console.log("== B. 折叠 / 延时 / 搁置 / 确认：抑制不丢数据，静音不是永久 ==");
{
  ok("B1 折叠窗是显式常量并在模块注释里写了取值理由（不是拍脑袋）",
     /GROUP_WINDOW_MS = 15_000/.test(al) &&
     /`GROUP_WINDOW_MS = 15s`：真机上一条 5005 在就绪流程里会/.test(al));
  ok("B2 窗内重复 → 一条活动报警 + count 累加；记 first_at / last_at",
     /else:\n\s+old\.count \+= 1/.test(al) &&
     /old\.last_at = ts/.test(al) &&
     /"count": rec\.count/.test(al) &&
     /"suppressed": max\(0, rec\.count - 1\)/.test(al));
  ok("B3 ★ 被折叠的条目逐条进明细（一条不丢），并带上审计事件号可回溯",
     /"folded"/.test(al) &&
     /def _log\(self, rec: Rec, ts: float, event[\s\S]{0,600}"event_id"/.test(al) &&
     /"count_at_this_point": rec\.count/.test(al) &&
     /durable_trail/.test(al));
  ok("B4 超过窗口再出现 = 又发生了一次（不并入旧计数；确认后再次出现也不是永久静音）",
     /if \(ts - old\.last_at\) \* 1000\.0 > GROUP_WINDOW_MS:\n\s+prev_acked = old\.acked[\s\S]{0,300}old\.count = 1/.test(al) &&
     /上一轮已确认后再次出现 = 又发生了一次/.test(al));
  ok("B5 分区键：不同 zone 不合并（两个区域被入侵是两条报警）",
     /def _scope_of\(rule: Rule/.test(al) &&
     /key = rule\.id \+ \(\("\|" \+ scope\) if scope else ""\)/.test(al) &&
     /if not rule\.scope_fields:\n\s+return ""/.test(al) &&
     /scope_fields=\("zone_id", "zone_name"\)/.test(al));  ok("B6 后果与根因分开成报警（下发被拦 ≠ 控制器报警 5005）",
     /def rules_for_event\([\s\S]{0,700}out: List\[Tuple\[Rule, Dict\[str, Any\]\]\] = \[\(main, detail\)\]/.test(al) &&
     /if r\.id != main\.id:\n\s+out\.append\(\(r, detail\)\)/.test(al));
  ok("B7 on-delay：抖动型先 pending，连续存在 ≥ delay_ms 才升级",
     /pending=\(rule\.delay_ms > 0\)/.test(al) &&
     /if old\.pending and old\.delay_ms > 0 and \(ts - old\.first_at\) \* 1000\.0 >= old\.delay_ms:\n\s+old\.pending = False/.test(al));
  ok("B8 候选态过期不冒充活动报警（只在明细留痕），且只有抖动型走延时",
     /def _expire_pending\([\s\S]{0,500}"pending_expired"/.test(al) &&
     /delay_ms=2_000,\n\s+clear_actions=\("connection\.restored",\)\)/.test(al));
  ok("B9 ★ 安全类禁止搁置：shelve() 就地抛 AlarmOpError 并写清为什么",
     /if rec\.safety:\n\s+raise AlarmOpError\(\n\s+"安全类报警禁止搁置/.test(al));
  ok("B10 搁置默认时长 + 上下界 + 到期自动回活动态（不是永久静音）",
     /SHELVE_DEFAULT_MIN = 30/.test(al) &&
     /mins = float\(minutes\) if minutes else SHELVE_DEFAULT_MIN/.test(al) &&
     /mins = max\(0\.1, min\(mins, 24 \* 60\)\)/.test(al) &&
     /def _expire_shelves\([\s\S]{0,500}"shelve_expired"/.test(al) &&
     /搁置到期 → 自动回到活动态/.test(al));
  ok("B11 自动清除只可能发生在登记了 auto_resolve_ms 的非安全类上",
     /if rec\.shelved or rec\.pending or rec\.safety:\n\s+continue/.test(al) &&
     /if ttl and \(ts - rec\.last_at\) \* 1000\.0 > ttl:/.test(al));
  ok("B12 ★ 确认 = 只清显示：文案与审计载荷都明写不解除任何互锁 / 就绪闸门",
     /确认只清显示，不解除任何互锁/.test(al) &&
     /"does_not_release": \["safety_guard\.check", "motion\.readiness_reason"/.test(al) &&
     /同一报警再次出现会重新成为活动报警/.test(al));
  ok("B13 水位只增不减：任何状态变化都 _bump；_seq = 0 只出现在构造与 reset",
     /def _bump\(self\)[\s\S]{0,80}self\._seq \+= 1/.test(al) &&
     (al.match(/self\._seq = 0/g) || []).length === 2 &&
     /def reset\(self\) -> None:[\s\S]{0,300}self\._seq = 0/.test(al) &&
     /"latest_seq": self\.latest_seq\(\)/.test(al));
  ok("B14 台账与明细都有界（7×24 进程不被撑爆），裁掉的仍留明细",
     /deque\(maxlen=HISTORY_MAX\)/.test(al) &&
     /MAX_ACTIVE = 200/.test(al) &&
     /报警台账超过 %d 条，折叠了最久没有再出现的条目/.test(al));
  ok("B15 自己的审计不回吃（emit→ingest→emit 递归被断路）",
     /AUDIT_ACTIONS = \("alarms\.ack", "alarms\.shelve", "alarms\.unshelve",/.test(al) &&
     /action in AUDIT_ACTIONS or action\.startswith\("alarms\."\)/.test(al));
  ok("B16 info/debug 是流水不是报警（只有 warn/error/critical 进台账）",
     /ALARM_LEVELS = \("warn", "error", "critical"\)/.test(al) &&
     /if str\(event\.get\("level"\) or ""\) not in ALARM_LEVELS:/.test(al));
  ok("B17 报警码只从结构化字段与报警语境里认，别把「第 3 次 / 40103」当报警码",
     /def extract_codes\([\s\S]{0,900}if \("报警" in txt\) or \("alarm" in txt\.lower\(\)\):/.test(al) &&
     /_CODE_RE = re\.compile\(r"\(\?<!\\d\)\(\\d\{4\}\)\(\?!\\d\)"\)/.test(al) &&
     !/_CODE_RE = re\.compile\(r"\(\\d\{4\}\)"\)/.test(al) &&
     /其它数字（第 3 次、15s、寄存器号 40103）/.test(al));
}

// ---------------------------------------------------------------------
console.log("");
console.log("== C. REST：读免令牌（观察也是安全要求），写要令牌，拒绝如实 ==");
{
  ok("C1 三条读 + 四条写路由齐了",
     /@router\.get\(""\)/.test(apiAl) && /@router\.get\("\/history"\)/.test(apiAl) &&
     /@router\.get\("\/dictionary"\)/.test(apiAl) && /@router\.post\("\/ack"\)/.test(apiAl) &&
     /@router\.post\("\/shelve"\)/.test(apiAl) && /@router\.post\("\/unshelve"\)/.test(apiAl) &&
     /@router\.post\("\/priority"\)/.test(apiAl));
  ok("C2 ★ 读接口不要求令牌（没登录的观察者也必须看得见围栏 hit，否则就是假安静）",
     (apiAlCode.match(/@router\.get\("[^"]*"\)[\s\S]{0,240}Depends\(require_/g) || []).length === 0);
  ok("C3 写接口一律 require_control；改优先级再收一档只给管理员",
     (apiAl.match(/Depends\(require_control\)/g) || []).length === 3 &&
     /@router\.post\("\/priority"\)[\s\S]{0,120}Depends\(require_admin\)/.test(apiAl));
  ok("C4 ★ 安全类禁止搁置是如实 409 + 原因，不装作「成功但没生效」",
     /except alarms\.AlarmOpError as e:[\s\S]{0,120}raise HTTPException\(status_code=409, detail=str\(e\)\)/.test(apiAl) &&
     (apiAl.match(/status_code=409/g) || []).length >= 3);
  ok("C5 报警已经不在了 → 404 并说明去哪儿查（明细），不静默 200",
     /except KeyError:\n\s+raise _reject\(body\.key\)/.test(apiAl) &&
     /status_code=404[\s\S]{0,160}已不在活动列表里/.test(apiAl));
  ok("C6 响应带 latest_seq（水位）与折叠窗 / 搁置默认值（前端不硬编码）",
     /"latest_seq": s\["latest_seq"\]/.test(apiAl) &&
     /"window_ms": alarms\.GROUP_WINDOW_MS/.test(apiAl) &&
     /"shelve_default_min": alarms\.SHELVE_DEFAULT_MIN/.test(apiAl));
  ok("C7 ★ 读响应里不许用列表长度当水位（total 只是条数，水位另有其值）",
     /# ★ 水位用 latest_seq（只增不减）[\s\S]{0,400}"latest_seq": s\["latest_seq"\]/.test(apiAl) &&
     !/"latest_seq": *len\(/.test(apiAl) && !/"latest_seq": *len\(/.test(al));
  ok("C8 路由已挂进 main.py（只挂路由，不引判据）",
     /^\s+alarms,$/m.test(mainPy) &&
     /app\.include_router\(alarms\.router\)/.test(mainPy));
}

// ---------------------------------------------------------------------
console.log("");
console.log("== D. 审计：唯一出口 events.emit + 只用已存在的合法 category ==");
{
  ok("D1 审计只经 app/services/events.py::emit（不自己写库、不自己广播）",
     /from app\.services\.events import emit as emit_event\n\s+emit_event\(/.test(al));
  ok("D2 ★ category 用的是既有的 control —— 没有为报警新增一个 category",
     /emit_event\("control", level, action, message, detail, actor=/.test(al) &&
     /CATEGORIES = \("system", "connection", "auth", "control", "config", "safety", "vision"\)/.test(evs));
  ok("D3 四类人操作都出审计：ack / shelve / unshelve / priority（外加自动清除）",
     /_queue_audit\(\n\s+"alarms\.ack"/.test(al) && /_queue_audit\(\n\s+"alarms\.shelve"/.test(al) &&
     /_queue_audit\("alarms\.unshelve"/.test(al) && /_queue_audit\(\n\s+"alarms\.priority"/.test(al) &&
     /"alarms\.auto_clear"/.test(al));
  ok("D4 审计先排队、出锁再写（不拿着报警锁去开 DB 会话）；写失败也不带崩主流程",
     /def _queue_audit\([\s\S]{0,200}self\._audit_q\.append/.test(al) &&
     /def _flush_audits\(self\)[\s\S]{0,120}q, self\._audit_q = self\._audit_q, \[\]/.test(al) &&
     /except Exception:\n\s+pass/.test(al));
  ok("D5 操作者取令牌角色，拿不到就如实写 unknown（不冒充 system）",
     /def who_actor\([\s\S]{0,200}return a if a in \("admin", "operator"\) else "unknown"/.test(al) &&
     /return token_role\(tok\) or "unknown"/.test(apiAl));
}

// ---------------------------------------------------------------------
console.log("");
console.log("== E. 边界：显示层就是显示层，绝不变成第二套安全判据 ==");
{
  ok("E1 ★ 报警层是事件总线上的观察者：钩子放在最后、整段包在 try 里、只读 payload",
     /try:\n\s+from app\.services import alarms\n\s+alarms\.ingest\(payload\)/.test(evs) &&
     /except Exception as e:[\s\S]{0,60}\n\s+log\.debug\("报警层摄入失败\(已忽略\)/.test(evs));
  ok("E2 报警层不 import 任何写路径（motion/modbus/jog/rc_ready/sim_robot/camera_client/互锁）",
     !/from app\.services\.(motion|modbus|jog|rc_ready|sim_robot|camera_client|safety_guard|safety_eval|limits|ui_speed)/.test(alCode) &&
     !/import (motion|modbus|jog|rc_ready|sim_robot|camera_client|safety_guard|safety_eval)\b/.test(alCode));
  ok("E3 ★ 互锁 / 下发 / 采集 / 就绪侧对本层一无所知（反向不依赖，钉五个文件）",
     !/alarms/.test(stripPy(guardPy)) && !/alarms/.test(stripPy(evalPy)) &&
     !/alarms/.test(stripPy(motionPy)) && !/alarms/.test(stripPy(modbusPy)) &&
     !/alarms/.test(stripPy(jogPy)) && !/alarms/.test(stripPy(rcPy)));
  ok("E4 报警层自己没有下发形状（不 command / 不 jog / 不写寄存器 / 不急停）",
     !/\.command\(|rc_jog|rc_vacuum|\.estop\(|write_register|\.move_to\(|apiControl/.test(alCode));
  ok("E5 纯内存判据：不碰 DB 会话（只经 events.emit 出审计）",
     !/SessionLocal|get_db\(|sqlite3|sqlalchemy/.test(alCode));
  ok("E6 全仓消费者只有四个（events 钩子只 ingest / api 入口读写 / main 只挂路由 / 日报只读）"
     + " —— 类方法 4 个 + 模块级转发 4 个，别多一个能改报警的入口",
     (al.match(/^    def (ack|shelve|unshelve|set_priority)\(/gm) || []).length === 4 &&
     (al.match(/^def (ack|shelve|unshelve|set_priority)\(/gm) || []).length === 4 &&
     /alarms\.ingest\(/.test(read("../backend/app/services/events.py")) &&
     !/alarms\.(ack|shelve|unshelve|set_priority)\(/.test(read("../backend/app/services/events.py")) &&
     !/alarms\.(ack|shelve|unshelve|set_priority)\(/.test(read("../backend/app/main.py")) &&
     /alarms\.(summary|stats|history)\(/.test(read("../backend/app/services/daily_report.py")) &&
     !/alarms\.(ack|shelve|unshelve|set_priority|ingest)\(/.test(stripPy(read("../backend/app/services/daily_report.py"))));
  ok("E7 ★ 核心反证测试在，且没被删空（搁置/确认绝不改变 safety_guard.check 的返回）",
     /def test_shelving_never_affects_guard_check\(\)/.test(tests) &&
     /SG\.check\(\)/.test(tests) &&
     /def test_safety_alarm_is_active_immediately_never_delayed\(\)/.test(tests) &&
     /def test_priority_never_downgraded_by_repetition\(\)/.test(tests) &&
     /def test_alarm_layer_is_not_consumed_by_any_safety_or_motion_module\(\)/.test(tests) &&
     /def test_alarm_layer_never_breaks_the_event_bus/.test(tests));
  ok("E8 35 条回归钉都在（六件事逐件有钉，含 REST 四条）",
     (tests.match(/^def test_/gm) || []).length >= 35,
     "实际 " + (tests.match(/^def test_/gm) || []).length + " 条");
}

// ---------------------------------------------------------------------
console.log("");
console.log("== F. 界面：ISA-101 克制配色 + 状态以后端为准 + 刹车不受影响 ==");
{
  ok("F1 ★ 刷新水位用后端 latest_seq，不用列表长度（§5.5 铁律①）",
     /const seq = Number\(d\.latest_seq \|\| 0\)/.test(comp) &&
     /if \(!force && seq === alarmSeq\.value\)/.test(comp) &&
     !/alarms\.value\.length === alarmSeq/.test(comp) &&
     /不看列表长度/.test(comp));
  ok("F2 水位变小 → 如实说「后端重启过，台账是内存态」，不静默重置",
     /if \(seq < alarmSeq\.value\) \{[\s\S]{0,200}水位从 #\$\{alarmSeq\.value\} 回到 #\$\{seq\}/.test(comp));
  ok("F3 ★ 读失败保留上一次列表并明写「读不到 ≠ 没有报警」（不空成一切正常）",
     /alarmStale\.value = `报警台账读取失败[\s\S]{0,120}不等于「没有报警」`/.test(comp) &&
     /读不到 ≠ 没有报警/.test(comp) &&
     !/alarms\.value = \[\]/.test(compCode));
  ok("F4 从没读到过 = 未知（v-if=!alarmRead 那一支），不是「没有报警」",
     /v-if="!alarmRead" class="ops-empty"/.test(comp) &&
     /v-if="!alarmRead" class="ops-chip">状态未知<\/span>/.test(comp));
  ok("F5 ★ 三态不塌成布尔：pending/shelved 逐档给词，读不到就说状态未知",
     /if \(a\.pending === null \|\| a\.pending === undefined\) return "状态未知"/.test(comp) &&
     /if \(a\.pending === true\) return "候选中/.test(comp) &&
     /if \(a\.shelved === true\) return "已搁置"/.test(comp));
  ok("F6 每一档同时给中文标签（紧急/高/中/低/未分级）—— 看不见颜色也读得懂",
     /critical: \{ t: "紧急", cls: "p-critical" \}/.test(comp) &&
     /unclassified: \{ t: "未分级", cls: "p-unknown" \}/.test(comp) &&
     /prioText\(p\)/.test(comp));
  ok("F7 ★ ISA-101 克制配色：critical=红 / high=橙 / medium=黄 / low=蓝灰 / 未分级=虚线中性灰",
     /\.al-item\.p-critical \{ border-left-color: var\(--err\)/.test(comp) &&
     /\.al-prio\.p-high \{ color: var\(--bg\); background: #e8873a/.test(comp) &&
     /\.al-prio\.p-medium \{ color: var\(--bg\); background: #d9c14a/.test(comp) &&
     /\.al-prio\.p-low \{ color: var\(--bg\); background: #8ea3c4/.test(comp) &&
     /\.al-prio\.p-unknown \{[\s\S]{0,80}border-style: dashed/.test(comp));
  ok("F8 ★ 没有任何闪动 / 动画（剥注释后判）：闪动在控制室是干扰源",
     !/animation\s*:|@keyframes|blink|\.al-prio[^{]*\{[^}]*transition/.test(compCode));
  ok("F9 状态一律后端给的字段：can_shelve / priority_source / count 直接吃，前端不复算判据",
     /:disabled="alarmBusy \|\| a\.can_shelve === false"/.test(comp) &&
     /安全类报警禁止搁置（后端判据，前端不复算）/.test(comp) &&
     /a\.priority_source === 'operator'/.test(comp) &&
     /×\{\{ a\.count \?\? "—" \}\}/.test(comp));
  ok("F10 确认前问清「只清显示，互锁照旧」；409 的原因原样贴出（不写「操作失败」糊弄）",
     /确认只把它从列表里清掉，不解除任何互锁/.test(comp) &&
     /alarmMsg\.value = r\.ok \? okText : \(d\.message \|\| `操作被拒绝（HTTP \$\{r\.status\}）`\)/.test(comp));
  ok("F11 写操作走 apiControl（带控制令牌），被 401/403 拒绝时拉起登录而不是发呆",
     /const r = await apiControl\(path, \{/.test(comp) &&
     /if \(r\.status === 401 \|\| r\.status === 403\) \{[\s\S]{0,160}auth\.requestLogin\(\)/.test(comp));
  // ★ 判据只看 loadAlarms **函数体内部**（"离得近"不算门控：注释与相邻的
  //   `watch(() => auth.controlActive, …)` 会在 240 字符窗口里造成假命中）。
  const laStart = compCode.indexOf("async function loadAlarms(");
  const laBody = laStart < 0 ? "" : compCode.slice(laStart, compCode.indexOf("\n}", laStart));
  ok("F12 读接口在前端也不卡令牌（函数体内没有 controlActive 门控；没登录也看得见围栏 hit）",
     /const r = await fetch\(apiUrl\("\/alarms"\)\)/.test(comp) &&
     laStart >= 0 && laBody.length > 80 && !/controlActive/.test(laBody));
  ok("F13 被折叠的原始条目界面可达（按 key 拉明细，带 event_id 与逐态用词）",
     /\/alarms\/history\?key=\$\{encodeURIComponent\(a\.key\)\}/.test(comp) &&
     /folded: "折叠计数"/.test(comp) &&
     /shelve_expired: "搁置到期"/.test(comp) &&
     /#\\\{ h\.event_id \}|\{h\.event_id\}|>#\{\{ h\.event_id \}\}/.test(comp));
  ok("F14 搁置默认分钟取自后端（不是前端硬编码 30）",
     /shelveMin\.value = Number\(alarmMeta\.value\.shelve_default_min \|\| 30\)/.test(comp));
  ok("F15 ★ 「刹车」不受报警层影响：exec.js 里既没有报警台账也没有限制急停的条件",
     /apiControl\("\/control\/estop"/.test(execJs) &&
     !/alarm|alarmSeq|can_shelve|ops-sec/.test(execJs));
  ok("F16 没有新导航 / 没改 index.html（报警台账就在运维页里）",
     !/alarms/.test(read("index.html")) &&
     (["src/App.vue", "src/config.js", "src/net/control.js"].filter((f) => /\/api\/alarms|"\/alarms\/ack"/.test(read(f))).length === 0));
  // ★ 只判**看得见**的文案（模板区）+ 后端登记文本；JS 注释里用 ASCII 引号不是隐患
  //   （真正的隐患是「双引号串里再写双引号」→ 整页白屏，那条由 check_cjk_bare 的结构化
  //    词法守卫负责，它已挂在构建链上，所以下面第③条钉的是"守卫还在链上"）。
  const tplZone = comp.slice(comp.indexOf("<template>"), comp.lastIndexOf("</template>"));
  const docBodies = (al.match(/"""[\s\S]*?"""/g) || [])
    .map((t) => t.replace(/^"""|"""$/g, "")).join(String.fromCharCode(10));
  ok("F17 看得见的中文文案用「」不用 ASCII 双引号（模板区 + 后端文档串）；引号守卫在构建链上（src + tools）",
     !/[\u4e00-\u9fff]"[\u4e00-\u9fff]/.test(tplZone) &&
     !/[\u4e00-\u9fff]"/.test(docBodies) &&
     /node tools\/check_cjk_bare\.mjs src tools/.test(pkg));
}

// ---------------------------------------------------------------------
console.log("");
console.log("== G. 本脚本自身的接线状态（如实报告，不假装已挂链）==");
{
  ok("G1 本脚本已并入 `npm run verify` 链（不并入 = 钉了也永远不会跑，等于没钉）",
     /node tools\/verify_alarms\.mjs/.test(pkg));
  console.log("  INFO  报警层（C2）的守卫现在随 `npm run verify` 一起跑；"
              + "行为级的反证（搁置不影响互锁判据）在 backend/tests/test_alarms.py。");
}

console.log("");
console.log("结果: " + pass + " 通过 / " + fail + " 失败");
console.log("");
process.exit(fail ? 1 : 0);
