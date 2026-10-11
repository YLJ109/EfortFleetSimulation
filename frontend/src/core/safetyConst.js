/**
 * 安全常量：与后端 backend/app/core/safety_const.py 同源。
 *
 * ★ 口径（2026-09-25 用户明确）
 *   - 操作员在安全前提下 **J1–J6 全轴可控**；
 *   - 「仅 J6」不是一个常开的默认限制，而是一个**可开关的模式**
 *     （后端 motion.joint_lock.enabled，默认 false）；
 *   - AI 做真机验证时才开这个开关，且只动 J6、速度固定 5%。
 *
 * 因此这里的 DEFAULT_LOCKED_JOINTS 只是"模式开启时"的默认值，
 * **运行时一律以后端 /api/system/health 的 joint_lock 为准**。
 *
 * ★ SPEED_MIN = 5 是工程边界，不是"永远只能跑 5%"。
 *   操作员要跑多快由操作员决定；AI 的 5% 属操作纪律。
 */
export const SPEED_MIN = 5;
export const SPEED_MAX = 100;
// ★ 2026-10-11 用户口径：**每次启动默认 10%**（原来 5%）。
//   下限仍 SPEED_MIN(=5)：需要更慢时还能往下拖。★ 默认值 5%→10% = 真机**提速一倍**，
//   属用户明确要求；滑块值 1:1 直通到 40103（由 robot.yaml 的 service_program_vperc=100 保证）。
export const SPEED_DEFAULT = 10;
export const JOG_SPEED_MIN = 5; // °/s
export const JOG_SPEED_MAX = 10; // °/s

export const DEFAULT_LOCKED_JOINTS = Object.freeze([1, 2, 3, 4, 5]);
export const DEFAULT_ONLY_JOINT = 6;
export const JOINT_LOCK_TOL_DEG = 0.5;

/** 把速度夹到工程边界内（与后端 clamp_speed 同语义）。 */
export function clampSpeed(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return SPEED_MIN;
  return Math.max(SPEED_MIN, Math.min(SPEED_MAX, Math.round(n)));
}

/** AI 真机验证专用：恒定 5%，且不依赖任何外部状态。 */
export const AI_TEST_SPEED = 5;
/** AI 真机验证专用：唯一可动轴。 */
export const AI_TEST_JOINT = 6;
