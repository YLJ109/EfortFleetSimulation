// =====================================================================
// 品牌信息（唯一事实来源）—— 顶部栏 logo/名称、页面标题、后端服务名共用一份。
//
// 命名说明（2026-10-10 用户改版）：
//   顶栏改成 **品牌名 + 副标题** 两行 ——
//     · 主标题（品牌）：**「擎云制造」**
//     · 副标题（描述性全名，小一号）：**「工业机器人远程智控与数字孪生系统」**
//   厂商/机型归属由 `device`（EFORT ER8-700H）承担（顶栏悬停可见）。
//   历史名「FIT 埃夫特智能机器人远程控制与监控系统」「FIT远程监控与操控机器人平台」均已弃用；
//   英文名保留厂商标识：FIT EFORT Intelligent Robot Remote Control & Monitoring System。
//
// ★ 改名字只改这一个文件 + backend/app/core/brand.py（后端，用于 /api/version 与 OpenAPI 标题）。
//   静态 index.html 的 <title> 也有一份（首屏 hydration 前显示），两边必须同步。
// ★ 缩写保持 FIT-RCMS 不变：它是导出文件名前缀（点位/程序/事件/配置包），
//   现场已经有一批导出文件带着这个前缀，为了加两个字就改前缀会让新旧文件对不上。
// =====================================================================

export const BRAND = {
  /** 厂商/团队前缀 */
  short: "FIT",
  /** 中文品牌名（顶部栏主标题） */
  name: "擎云制造",
  /** 顶部栏副标题（主标题下方，小一号）：描述性全名 */
  subtitle: "工业机器人远程智控与数字孪生系统",
  /** 英文全称（文档/导出文件用） */
  en: "FIT EFORT Intelligent Robot Remote Control & Monitoring System",
  /** 缩写（导出文件名、日志前缀） */
  abbr: "FIT-RCMS",
  /** 顶部栏悬停提示里的标语（型号 + 能做什么） */
  tagline: "实时监控 · 示教操控 · 安全互锁",
  /** 设备型号（顶栏悬停提示前缀；后续接第二台机器人时改这里或改成动态值） */
  device: "EFORT ER8-700H",
  /** 顶部栏 logo 图标（Icon.vue 里的键名） */
  logo: "robot",
  /** 导出文件名前缀（点位/程序/事件导出会拼上时间戳） */
  exportPrefix: "FIT-RCMS",
};

/** 顶栏悬停提示副串：型号 + 标语（顶栏可见的副标题是 BRAND.subtitle） */
export function brandSubtitle() {
  return `${BRAND.device} · ${BRAND.tagline}`;
}
