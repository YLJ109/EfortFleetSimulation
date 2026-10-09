// =====================================================================
// 品牌信息（唯一事实来源）—— 顶部栏 logo/名称、页面标题、后端服务名共用一份。
//
// 命名说明：现名 **「FIT 埃夫特智能机器人远程控制与监控系统」**
//   FIT（团队）+ 埃夫特（厂商/机型归属）+ 智能机器人 + 远程控制与监控 + 系统，
//   从"谁做的 / 管哪台设备 / 干什么"三件事一次说清；
//   旧名「FIT远程监控与操控机器人平台」会在「操控机器人 / 平台」处可两读，已弃用；
//   英文 FIT EFORT Intelligent Robot Remote Control & Monitoring System。
//
// ★ 改名字只改这一个文件 + backend/app/core/brand.py（后端，用于 /api/version 与 OpenAPI 标题）。
//   静态 index.html 的 <title> 也有一份（首屏 hydration 前显示），两边必须同步。
// ★ 缩写保持 FIT-RCMS 不变：它是导出文件名前缀（点位/程序/事件/配置包），
//   现场已经有一批导出文件带着这个前缀，为了加两个字就改前缀会让新旧文件对不上。
// =====================================================================

export const BRAND = {
  /** 厂商/团队前缀 */
  short: "FIT",
  /** 中文全称（顶部栏显示） */
  name: "工业机器人远程智控与数字孪生系统",
  /** 英文全称（文档/导出文件用） */
  en: "FIT EFORT Intelligent Robot Remote Control & Monitoring System",
  /** 缩写（导出文件名、日志前缀） */
  abbr: "FIT-RCMS",
  /** 顶部栏副标题：一眼看出这是给哪台设备用的、能做什么 */
  tagline: "实时监控 · 示教操控 · 安全互锁",
  /** 设备型号（副标题前缀；后续接第二台机器人时改这里或改成动态值） */
  device: "EFORT ER8-700H",
  /** 顶部栏 logo 图标（Icon.vue 里的键名） */
  logo: "robot",
  /** 导出文件名前缀（点位/程序/事件导出会拼上时间戳） */
  exportPrefix: "FIT-RCMS",
};

/** 顶部栏副标题：型号 + 标语 */
export function brandSubtitle() {
  return `${BRAND.device} · ${BRAND.tagline}`;
}
