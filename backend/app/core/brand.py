# -*- coding: utf-8 -*-
"""
品牌与服务标识（唯一事实来源）。

前端对应文件：`frontend/src/brand.js`。两边名称必须一致 —— 后端出现在
`/api/version`、`/api/system/health`、导出包头部、OpenAPI 文档标题里，
前端出现在顶部栏与页面标题里，对不上会让现场以为是两套系统。

命名说明（2026-10-10 用户改版）：项目名「擎云制造」——
顶栏主标题是品牌名「擎云制造」，其下副标题（描述性全名）为「工业机器人远程智控与数字孪生系统」。
厂商/机型归属由 DEVICE_MODEL（EFORT ER8-700H）承担。
历史名「FIT 埃夫特智能机器人远程控制与监控系统」「FIT远程监控与操控机器人平台」均已弃用。
缩写保持 FIT-RCMS 不变（它是导出文件名前缀，现场已有带该前缀的导出文件）。
"""

SERVICE_NAME = "擎云制造"
# 描述性全名（顶栏副标题 / OpenAPI 描述用）
SERVICE_SUBTITLE = "工业机器人远程智控与数字孪生系统"
SERVICE_NAME_EN = "FIT EFORT Intelligent Robot Remote Control & Monitoring System"
SERVICE_ABBR = "FIT-RCMS"
SERVICE_VERSION = "0.4.0"
DEVICE_MODEL = "EFORT ER8-700H"

# 导出文件名前缀（点位/程序/事件/配置包导出统一用它）
EXPORT_PREFIX = SERVICE_ABBR

# 配置包格式标识（/api/system/export 的 format 字段，导入时校验）
EXPORT_FORMAT = "efort-system/v1"
