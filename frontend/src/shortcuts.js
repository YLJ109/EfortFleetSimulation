// =====================================================================
// 全局键盘快捷键 —— 纯逻辑（不碰 DOM / Vue），定义与解析都在这里。
//
// ★ 为什么单独一个文件：
//   键盘快捷键是"按下去就出事"的东西，必须能在 node 里把每一种上下文钉死：
//     - 在输入框里打字时，字母键与空格**绝不能**被劫持（否则在密码框里按 e，
//       侧栏开合一下；在点位名称里打空格，程序跑起来）；
//     - 弹层开着时 Esc 应该是"关掉它"，不是"急停"；
//     - 空格只在程序执行页有效，别处是"翻页"；
//     - 带 Ctrl/Alt/Meta 的组合键一概不抢（那是浏览器与系统的）。
//   这些判据写在 App.vue 里只能靠人肉点，抽出来才能断言（tools/verify_shortcuts.mjs）。
//
// ## 危险动作的取向
//   急停（Esc）**不弹确认框** —— 急停的价值就在于"按下去立刻停"，多一次点击就是
//   多一次犹豫。代价是可能误触，但误触的后果（停一下再复位）远小于"来不及停"。
//   复位（Enter）反过来要先确认当前**确实**处于急停锁定，避免在正常运行时误按。
// =====================================================================

/** 分组（「关于」页按这个顺序展示）。 */
export const SHORTCUT_GROUPS = ["导航", "视图", "执行", "安全"];

/**
 * 快捷键清单（唯一事实来源：解析表 + 「关于」页的说明都读它）。
 *   id     —— 动作标识，App.vue 按它分派
 *   keys   —— 触发键（KeyboardEvent.key 的小写形式；数字键用 1~9，范围见 TAB_KEY_COUNT）
 *   label  —— 界面上显示的键帽文字
 *   group  —— 分组
 *   desc   —— 一句话说明
 *   needAuth —— 需要控制令牌（没令牌时按下给提示，不静默失效）
 *   danger —— 危险动作：界面上用警示色，且要出提示
 *   when   —— 生效条件说明（给用户看）
 */
export const SHORTCUTS = [
  {
    id: "tab", keys: ["1", "2", "3", "4", "5", "6", "7", "8", "9"], label: "1 ~ 9",
    group: "导航", desc: "从左到右切到第 N 个模块（没有权限的模块不显示，序号顺延）",
    needAuth: false, danger: false, when: "任何页面",
  },
  {
    id: "toggle_side", keys: ["e"], label: "E",
    group: "视图", desc: "展开 / 收起侧面板（给 3D 视口让出宽度）",
    needAuth: false, danger: false, when: "任何页面",
  },
  {
    id: "toggle_camera", keys: ["q"], label: "Q",
    group: "视图", desc: "显示 / 隐藏摄像头画面",
    needAuth: false, danger: false, when: "任何页面",
  },
  {
    id: "program_run_or_stop", keys: [" "], label: "空格",
    group: "执行", desc: "运行 / 停止程序（空闲时重复运行上一次跑过的那一个，不会替你挑）",
    needAuth: true, danger: false, when: "仅在「程序执行」页",
  },
  {
    id: "estop", keys: ["escape"], label: "Esc",
    group: "安全", desc: "急停：立即停止一切运动并锁定",
    needAuth: true, danger: true, when: "任何页面（有弹层时先关弹层）",
  },
  {
    id: "estop_reset", keys: ["enter"], label: "Enter",
    group: "安全", desc: "复位急停，解除锁定",
    needAuth: true, danger: false, when: "任何页面（需先处于急停锁定）",
  },
];

/** id → 定义（供「关于」页与提示文案取描述）。 */
export const SHORTCUT_BY_ID = Object.fromEntries(SHORTCUTS.map((s) => [s.id, s]));

/** 数值键位数（= 顶栏可直达模块数，与 src/tabs.js 的 TAB_KEY_COUNT 必须一致）。 */
export const TAB_KEY_COUNT = 9;

const IGNORE_TAGS = { INPUT: 1, TEXTAREA: 1, SELECT: 1, OPTION: 1 };

/**
 * 焦点是否在"正在输入"的元素里。
 *   - 表单控件（input/textarea/select）→ 是；
 *   - contenteditable 区域 → 是（富文本编辑同样不能劫持）；
 *   - 元素自报 `data-keys="off"` → 是（例如 3D 视口里自己处理方向键的区域）。
 */
export function isTypingTarget(el) {
  if (!el) return false;
  const tag = String(el.tagName || "").toUpperCase();
  if (IGNORE_TAGS[tag]) return true;
  if (el.isContentEditable === true) return true;
  try {
    if (el.getAttribute && el.getAttribute("data-keys") === "off") return true;
  } catch (e) { /* 忽略 */ }
  return false;
}

/**
 * 把一次按键解析成动作 id。返回 `""` = 这次按键不触发任何动作。
 *
 * @param {{key?:string, ctrlKey?:boolean, metaKey?:boolean, altKey?:boolean, repeat?:boolean}} ev
 * @param {{typing?:boolean, modalOpen?:boolean, view?:string, stopped?:boolean}} ctx
 *   typing    焦点在输入控件里（见 isTypingTarget）
 *   modalOpen 有弹层开着（登录框 / 播报设置 / 状态灯浮层）
 *   view      当前页面 key
 *   stopped   是否处于急停锁定
 * @returns {string} 动作 id：`tab:3` / `toggle_side` / `estop` / … 或 ""
 */
export function resolveKey(ev, ctx = {}) {
  if (!ev) return "";
  // 带修饰键 = 用户在按组合键（Ctrl+R 刷新、Alt+Tab 切窗口…），一律不抢。
  if (ev.ctrlKey || ev.metaKey || ev.altKey) return "";
  // 输入中一律不劫持 —— ★ 这条必须排在最前，它是"打字打成急停"的唯一防线。
  if (ctx.typing) return "";

  const k = String(ev.key || "");

  // ---- 数字键切模块：范围由 TAB_KEY_COUNT **推导**，不再写死 ----
  //   ★ 踩过的坑：模块数从 7 加到 8 时忘了改这里的 [1-7]，按 8 完全没反应
  //     （由 tools/verify_shortcuts.mjs 的 D1/K3 抓到）。用长度 + 区间判断，
  //     既避开 "12" / "3abc" 这类多字符边界，又永远跟模块数一致。
  if (k.length === 1 && k >= "1" && k <= String(TAB_KEY_COUNT)) return "tab:" + k;

  const low = k.toLowerCase();
  if (low === "e") return ev.repeat ? "" : "toggle_side";
  if (low === "q") return ev.repeat ? "" : "toggle_camera";

  if (k === "Escape") {
    // ★ 优先级：弹层开着 → 关弹层。这是所有界面的通用预期；
    //   若这里直接急停，用户想关个窗口就把机器人拍停，之后会把 Esc 从肌肉记忆里删掉，
    //   真需要急停时反而不敢按 —— 那才是真正的安全隐患。
    return ctx.modalOpen ? "close_modal" : "estop";
  }

  if (k === "Enter") {
    if (ctx.modalOpen) return "submit_modal";
    // 复位必须先确认"确实锁着"：正常运行时误按 Enter 不该有任何副作用。
    return ctx.stopped ? "estop_reset" : "";
  }

  if (k === " ") {
    // 空格在别处是"向下翻页"，只在程序执行页才接管。
    return ctx.view === "program" ? "program_run_or_stop" : "";
  }

  return "";
}

/** 从 `tab:N` 取序号（1 起）；不是切模块动作返回 0。 */
export function tabIndexOf(action) {
  // ★ 范围由 TAB_KEY_COUNT 推导，不写死数字 —— 原先这里是 `/^tab:([1-7])$/`，
  //   模块加到 8 后 `tab:8` 解不出来（verify_shortcuts K3 抓到）。
  //   同一个坑在 resolveKey 里也有一份，两处都已改成推导。
  const s = String(action || "");
  if (s.slice(0, 4) !== "tab:") return 0;
  const body = s.slice(4);
  if (!/^\d+$/.test(body)) return 0;
  const n = Number(body);
  return n >= 1 && n <= TAB_KEY_COUNT ? n : 0;
}

/**
 * 按可见模块列表把 `tab:N` 解成具体的模块 key。
 * 越界（例如没权限只显示了 4 个模块，却按了 7）返回 ""。
 */
export function tabKeyFor(action, visibleKeys) {
  const i = tabIndexOf(action);
  if (!i) return "";
  const list = Array.isArray(visibleKeys) ? visibleKeys : [];
  return i <= list.length ? list[i - 1] : "";
}

/** 按分组整理成「关于」页要展示的行。 */
export function shortcutRows() {
  return SHORTCUT_GROUPS.map((g) => ({
    group: g,
    items: SHORTCUTS.filter((s) => s.group === g),
  })).filter((r) => r.items.length);
}
