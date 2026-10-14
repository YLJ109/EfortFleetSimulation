// =====================================================================
// ★ B1 前端渲染侧采样器（**默认关闭**，运维页勾选才采）
//
// 为什么要有它：在此之前"这个项目的前端性能"只有猜测（方案 §1.2）。
// 这里只采三样最有用的（方案「最小可交付切片」口径）：
//   ① 帧时（p50 / p95 / 最差）→ 由它导出 fps；
//   ② draw call / 三角形数 / geometries / textures（renderer.info）；
//   ③ **停帧累计秒数**（rAF 被浏览器/系统掐住的时长）。
//
// ★ 三条硬约束（都是本项目的老事故）：
// 1. **不进渲染热路径做重活**：每帧只做一次 Float32Array 写入（环形缓冲），
//    排序/分位数只在**被读取时**算（界面 1s 读一次）。开销本身要能显示 → `overhead`。
// 2. **停帧秒数取自单调时钟差**（`performance.now()`），不用 Date.now() ——
//    系统校时/休眠会让墙钟跳变，把"停帧"算成负数或几小时。
// 3. **没采到 = null，不是 0**：刚开启还没有样本时 fps/p50 一律 null，
//    界面写"—（还没采到样本）"。把"没数据"画成"0 fps"就是假显示（§5.5 铁律③）。
//
// 与后端 `/api/system/metrics` 的分工：后端报**进程级**（采集节拍/Modbus 失败率/
// WS 积压…），这里报**浏览器里看得见的**（帧时与 GPU 计数）。两边都不互相换算。
// =====================================================================

const CAP = 4096;              // 环形缓冲：60fps × ~68s
const STALL_MS = 250;          // 两帧之间超过这个间隔算"停帧"（≈ 掉 15 帧以上）

let enabled = false;
let buf = null;                // Float32Array(CAP)：帧时 ms
let head = 0;                  // 下一个写入位
let winStart = 0;              // 本轮开启的时刻（performance.now()）
let lastFrame = 0;             // 上一帧时刻（单调时钟）
let stalledMs = 0;             // 累计停帧（单调时钟差之和）
let worstGap = 0;              // 单次最长停帧
let frames = 0;                // 本轮记录的帧数（overhead 口径用）
let longTasks = 0;
let longTaskWorst = 0;
let po = null;                 // PerformanceObserver('longtask')
let lastInfo = null;           // renderer.info 快照（只在被读时取）
let lastInfoAt = 0;

/** 由 manager.js 的渲染循环每帧调用（dtSec = 上一帧到这一帧的秒数）；关闭状态直接返回。 */
export function recordFrame(nowMs, dtSec) {
  if (!enabled) return;
  if (lastFrame) {
    const gap = nowMs - lastFrame;
    if (gap > STALL_MS) { stalledMs += gap; if (gap > worstGap) worstGap = gap; }
  }
  lastFrame = nowMs;
  buf[head] = Math.min(2000, Math.max(0, dtSec * 1000));
  head = (head + 1) % CAP;
  frames += 1;
}

export function enable() {
  if (enabled) return;
  enabled = true;
  buf = new Float32Array(CAP);
  head = 0; frames = 0;
  winStart = performance.now();
  lastFrame = 0; stalledMs = 0; worstGap = 0;
  longTasks = 0; longTaskWorst = 0;
  try {
    if (typeof PerformanceObserver !== "undefined") {
      const supported = PerformanceObserver.supportedEntryTypes
        && PerformanceObserver.supportedEntryTypes.includes("longtask");
      if (supported) {
        po = new PerformanceObserver((list) => {
          for (const e of list.getEntries()) {
            longTasks += 1;
            if (e.duration > longTaskWorst) longTaskWorst = e.duration;
          }
        });
        po.observe({ entryTypes: ["longtask"] });
      }
    }
  } catch (e) { po = null; }                 // 不支持就当作没有这一项（summary 里如实 null）
}

export function disable() {
  if (!enabled) return;
  enabled = false;
  if (po) { try { po.disconnect(); } catch (e) { /* 已经断了 */ } po = null; }
  buf = null; lastInfo = null; lastInfoAt = 0;
}

export function samplerEnabled() { return enabled; }

/** manager.js 在每次被读时把 renderer.info 交出来（这里不保存引用，只拷计数）。 */
export function ingestRendererInfo(info) {
  if (!enabled || !info) return;
  const r = info.render || {}, m = info.memory || {};
  lastInfo = {
    calls: r.calls ?? null, triangles: r.triangles ?? null,
    geometries: m.geometries ?? null, textures: m.textures ?? null,
  };
  lastInfoAt = performance.now();
}

function windowSamples() {
  if (!enabled || !buf) return null;
  const have = Math.min(frames, CAP);
  const out = new Float32Array(have);
  // 从最旧开始按写入顺序取出（head 指向下一个写入位 → 最旧在 head-have）
  const start = (head - have + CAP * 2) % CAP;
  for (let i = 0; i < have; i++) out[i] = buf[(start + i) % CAP];
  return out;
}

function pct(sorted, p) {
  if (!sorted.length) return null;
  const i = Math.min(sorted.length - 1, Math.max(0, Math.round((sorted.length - 1) * p)));
  return Number(sorted[i].toFixed(2));
}

/**
 * 界面唯一读数据的入口。**每个数字都带采样时刻**（§5.5：不显示无时刻的数）。
 * 没有样本的项一律 null（不是 0），由界面渲染成"—（还没采到样本）"。
 */
export function summary() {
  const now = performance.now();
  if (!enabled) {
    return { enabled: false, at: Date.now(), note: "采样器未开启（默认关：开销与'偷偷在跑'都不该由界面背）" };
  }
  const w = windowSamples();
  let p50 = null, p95 = null, worst = null, fps = null, mean = null;
  if (w && w.length >= 2) {
    const s = Float32Array.from(w).sort();
    let sum = 0;
    for (const v of s) sum += v;
    mean = Number((sum / s.length).toFixed(2));
    p50 = pct(s, 0.50); p95 = pct(s, 0.95);
    worst = Number(s[s.length - 1].toFixed(2));
    fps = mean > 0 ? Number((1000 / mean).toFixed(1)) : null;
  }
  const mem = (typeof performance !== "undefined" && performance.memory) || null;
  return {
    enabled: true,
    at: Date.now(),
    perfNow: now,
    windowSec: Number(((w ? now - winStart : 0) / 1000).toFixed(1)),
    frames,
    samples: w ? w.length : 0,
    fps, frame_mean_ms: mean, frame_p50_ms: p50, frame_p95_ms: p95, frame_worst_ms: worst,
    stalled_sec: Number((stalledMs / 1000).toFixed(2)),
    worst_stall_ms: Number(worstGap.toFixed(0)),
    stall_threshold_ms: STALL_MS,
    long_tasks: longTasks,
    long_task_worst_ms: longTasks ? Number(longTaskWorst.toFixed(0)) : null,
    heap_used_mb: mem ? Number((mem.usedJSHeapSize / 1048576).toFixed(1)) : null,
    heap_limit_mb: mem ? Number((mem.jsHeapSizeLimit / 1048576).toFixed(0)) : null,
    // renderer.info 是"最后一次读取时"的快照（不是每帧抓 —— 那才是热路径开销）
    draw_calls: lastInfo ? lastInfo.calls : null,
    triangles: lastInfo ? lastInfo.triangles : null,
    geometries: lastInfo ? lastInfo.geometries : null,
    textures: lastInfo ? lastInfo.textures : null,
    info_age_sec: lastInfo ? Number(((now - lastInfoAt) / 1000).toFixed(1)) : null,
    note: "帧时来自渲染循环的单调时钟差；停帧 = 两帧间隔 > " + STALL_MS + "ms 的累计",
  };
}

/** 给回归钉用的源码字面量（别改这些名字，verify_metrics.mjs 认它们）。 */
export const SAMPLER_CONTRACT = { CAP, STALL_MS };
