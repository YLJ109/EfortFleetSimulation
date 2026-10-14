// =====================================================================
// ★ C3「当前值 + 目标值 + 偏差 + 短趋势」的趋势底座：**纯前端环形缓冲**
//
// 方案：docs/方案-创新功能四批（…）.md §C3 —— 原话「不为趋势新增后端存储（用前端环形缓冲）；
// 不显示无法标注来源的数」。本文件就是那条"不新增后端存储"的实现处。
//
// 三条口径（别在别处另写一套）：
//   ① **只由 App 的 1 秒时钟采样**（`App.vue` 那个无条件推进的 tick）。
//      不自己 setInterval：多标签页 / 页面隐藏时会出现两份不同步的时钟（A6 踩过的坑）。
//   ② **未知就是未知**：`null` / `NaN` / 非有限数**不入环**，`latest()` 返回 `null`。
//      绝不用 0 顶替 —— §5.5 铁律③「未知不得塌成布尔/数值」在这里的同一条。
//   ③ 每个键的样本带 `at`（采集时刻），画趋势时按**时间窗**取，不按"最近 N 个"取：
//      WS 卡住时"最近 N 个"可能是 3 分钟前的数据，画出来就像"刚刚还正常"（假显示）。
//
// 运行：cd frontend && node tools/verify_param_quad.mjs（纯逻辑，无需浏览器）
// =====================================================================

/** 趋势窗口：60 秒（方案 §C3 给速度卡片定的口径；围栏那侧传 30s 覆盖）。 */
export const TREND_WINDOW_MS = 60000;
/** 每个键最多留多少个样本（60s @1s + 一倍余量；再老的自然掉出去）。 */
export const TREND_CAP = 128;

const rings = new Map();          // key → [{ v, at }]（at 升序）

/**
 * 唯一的一次数值转换：`null` / `undefined` / 布尔 / 空串一律给 NaN（= 未知）。
 * ★ 绝不能图省事写 `Number(v)` —— `Number(null) === 0` 会把"读不到"变成一个
 *   看起来很真实的 0 样本（§5.5 铁律③）。数字字符串照常接受（WS 里出现过字符串数）。
 */
function coerce(v) {
  if (typeof v === "number") return v;
  if (typeof v === "string" && v.trim() !== "") return Number(v);
  return NaN;
}

/**
 * 采一个点。`v` 必须是有限数，否则**整次调用什么都不做**（返回 false），
 * 调用方不必自己判空 —— 少一处"前端把未知当 0"的机会。
 *
 * ★★ `null` / `undefined` / `""` **必须在 Number() 之前挡掉**：
 *   `Number(null)` 是 **0** —— 若靠 `Number.isFinite(Number(v))` 判，"读不到"会被
 *   悄悄采成一个真实的 0 样本，时间窗里就多出一个"那一刻是 0%"的假点
 *   （§5.5 铁律③「未知不得塌成数值」在趋势这一侧的同一条）。数字字符串照常接受。
 */
export function sample(key, v, at = Date.now()) {
  if (!key) return false;
  const n = coerce(v);
  if (!Number.isFinite(n)) return false;
  let ring = rings.get(key);
  if (!ring) rings.set(key, (ring = []));
  ring.push({ v: n, at });
  if (ring.length > TREND_CAP) ring.splice(0, ring.length - TREND_CAP);
  // 掉出窗口的老样本一并清掉（否则 reset 前内存里全是历史）
  const cut = at - Math.max(TREND_WINDOW_MS, 1000) * 2;
  while (ring.length && ring[0].at < cut) ring.shift();
  return true;
}

/** 窗口内的样本（升序）。默认按"现在"取 60s；测试/其它窗口可传 windowMs / now。 */
export function series(key, windowMs = TREND_WINDOW_MS, now = Date.now()) {
  const ring = rings.get(key);
  if (!ring || !ring.length) return [];
  const from = now - windowMs;
  return ring.filter((p) => p.at >= from);
}

/** 最近一个有效样本；没有就返回 null（**不是 0、不是 {v:0}**）。 */
export function latest(key, windowMs = TREND_WINDOW_MS, now = Date.now()) {
  const s = series(key, windowMs, now);
  return s.length ? s[s.length - 1] : null;
}

/** 窗口内极值（给"60s 最大"这类指标用）；没数据返回 null。 */
export function extremes(key, windowMs = TREND_WINDOW_MS, now = Date.now()) {
  const s = series(key, windowMs, now);
  if (!s.length) return null;
  let min = s[0].v, max = s[0].v;
  for (const p of s) { if (p.v < min) min = p.v; if (p.v > max) max = p.v; }
  return { min, max, n: s.length };
}

/**
 * SVG `polyline points` 用的坐标串。**样本不足 2 个一律返回 null** ——
 * 单点画成一条横线会被读成"这一分钟都很平"，那是编故事。
 *
 * 纵轴默认按**窗口内自身极值**定标（看着有起伏）；传 `domain=[lo,hi]` 则按固定量程
 * 定标（用于"目标线在同一张图上要能横向比较"的场合，例如速度 0~100）。
 */
export function sparkPoints(key, w = 60, h = 16, opts = {}) {
  const now = opts.now === undefined ? Date.now() : opts.now;
  const s = series(key, opts.windowMs === undefined ? TREND_WINDOW_MS : opts.windowMs, now);
  if (s.length < 2) return null;
  let lo, hi;
  if (Array.isArray(opts.domain) && opts.domain.length === 2) {
    lo = opts.domain[0]; hi = opts.domain[1];
  } else {
    lo = s[0].v; hi = s[0].v;
    for (const p of s) { if (p.v < lo) lo = p.v; if (p.v > hi) hi = p.v; }
    if (lo === hi) { lo -= 1; hi += 1; }        // 完全平：给一点余量，别除零
  }
  const span = hi - lo || 1;
  const t0 = s[0].at, t1 = s[s.length - 1].at, dt = t1 - t0 || 1;
  const pad = 1;
  return s.map((p) => {
    const x = pad + ((p.at - t0) / dt) * (w - pad * 2);
    const y = pad + (1 - (p.v - lo) / span) * (h - pad * 2);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
}

/** 该键窗口内有没有足够画出趋势的样本（组件用它决定"画 sparkline"还是"如实说没数据"）。 */
export function hasTrend(key, windowMs = TREND_WINDOW_MS, now = Date.now()) {
  return series(key, windowMs, now).length >= 2;
}

/** 清空（单测隔离用；也供退出登录/换机时复位，避免拿着上一台机器的趋势画）。 */
export function reset(key) {
  if (key === undefined) rings.clear();
  else rings.delete(key);
}

/** 现有键名（调试/自检用）。 */
export function keys() {
  return [...rings.keys()];
}

/**
 * ★ C3：**环形角差** —— 转动轴（J6）整圈是**同一姿态**，直接相减会把"差 1°"显示成"差 359°"。
 *   折到 ±180 内（边界上 180 与 −180 是同一姿态，这里取 −180）：
 *   正数 = 实读在目标"正向那一侧"，负数 = 反向那一侧。
 *   与后端 `services/sorting.py::current_raw_slot` 的"J6 按环形差 ±360 等价"同一件事
 *   （那里判到位、这里只做显示换算，**两处都不许各写一份折圈算法** ⇒ 统一走这里）。
 *   非有限数返回 null（未知不塌成 0）。
 */
export function wrapAngleDev(d) {
  const n = coerce(d);
  if (!Number.isFinite(n)) return null;
  return ((n % 360) + 540) % 360 - 180;
}
