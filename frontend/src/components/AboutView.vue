﻿<script setup>
// =====================================================================
// 「关于」页：实验作业指导 + 使用说明 + 键盘快捷键 + 系统信息。
//
// 定位：现场"新同事上手"、"忘了某个快捷键"、"实验前该查什么"的第一站。
// 结构（按现场真实动作顺序排）：
//   1) 快速上手      —— 从启动服务到能干活，按顺序列步骤（不是功能清单）；
//   2) 实验前检查清单 —— 可勾选、带进度，关键项未确认会标红；
//   3) 标准操作流程   —— 7 段 SOP，折叠展开，危险步骤单独标红；
//   4) 注意事项       —— 按影响面分组，按 level 上色；
//   5) 异常处置速查   —— 现象→原因→处置，可搜索；
//   6) 快捷键 / 模块 / 安全须知 / 系统信息。
//
// ★ 快捷键表与 src/shortcuts.js **同源**：改快捷键只改那一个文件，本页自动跟随，
//   不会出现"说明上写着 E，实际按下去没反应"。
// ★ 模块说明与 tabs.js 的 TABS 键集合由 tools/verify_about.mjs 断言一致。
// ★ 本文件中文文案统一用「」，**不要**在双引号串里再写 ASCII 双引号 ——
//   会被 tools/check_cjk_bare.mjs 拦下（Vite 与 headless 测试都发现不了，只有浏览器白屏）。
// ★ 检查清单的勾选状态存 localStorage，刷新不丢；换人接手时可以一键重置。
// =====================================================================
import { computed, onMounted, reactive, ref } from "vue";
import Icon from "./Icon.vue";
import { BRAND } from "../brand.js";
import {
  QUICKSTART, MODULE_GUIDE, SAFETY_NOTES,
  CHECKLIST, PROCEDURES, PRECAUTIONS, TROUBLESHOOT,
} from "../guide.js";
import { shortcutRows } from "../shortcuts.js";
import { useSettingsStore } from "../stores/settings.js";
import { useLinkStore } from "../stores/link.js";
import { visibleTabs, TABS } from "../tabs.js";
import { useAuthStore } from "../stores/auth.js";

const auth = useAuthStore();
const settings = useSettingsStore();
const link = useLinkStore();

const rows = computed(() => shortcutRows());
const modules = computed(() => MODULE_GUIDE);
const live = computed(() => settings.live);

/** ★ Ab6：guide.js 数据里大量用 `**粗体**` markdown 记号，而本页是 `{{ }}` **纯文本**渲染 ——
 *  星号会原样显示在界面上（如「**灰度相机**」）。这里统一在渲染/复制入口去掉 `**`。
 *  ★ 不引入 v-html：这些文案不需要富文本，用 v-html 只会平白引入 XSS 面。 */
const plain = (v) => String(v == null ? "" : v).replace(/\*\*/g, "");

/** 当前权限下能用的模块（用来标注"需要权限"的模块现在能不能进）。 */
const enabledKeys = computed(() => visibleTabs(auth).map((t) => t.key));

// ★★ Ab1 修复（2026-10-11）：模块列表的数字编号必须与**顶栏数字键**一致。
//   原来直接用 MODULE_GUIDE 的下标 i+1 —— 而 MODULE_GUIDE 里第 3 项是 tabs.js 中
//   `hidden:true` 的「程序执行」（不在顶栏）。于是编号整体错位：读者按"3"以为切到
//   「程序执行」，实际顶栏的第 3 个是「智能分拣」。这是**误导性映射**。
//   现按顶栏真实顺序（navTabs，hidden 不占数字位）编号；hidden 项单独标注入口位置。
//   ★ 用静态的 TABS（不按权限过滤）保证编号在不同权限下稳定 —— 权限只影响能否进入，
//     不影响"这个模块在顶栏从左数是第几个"。
const NAV_KEYS = TABS.filter((t) => !t.hidden).map((t) => t.key);
/** 顶栏序号（1~8）；不在顶栏的（hidden，如「程序执行」）返回 0。 */
function modNo(key) { return NAV_KEYS.indexOf(key) + 1; }

// ---------------------------------------------------------------------
// 章节导航（页面变长后没有导航会很难找东西）
// ---------------------------------------------------------------------
const SECTIONS = [
  { id: "ab-sec-quick", label: "快速上手", icon: "flask" },
  { id: "ab-sec-check", label: "实验前检查", icon: "check" },
  { id: "ab-sec-sop", label: "操作流程", icon: "play" },
  { id: "ab-sec-prec", label: "注意事项", icon: "alert" },
  { id: "ab-sec-trouble", label: "异常速查", icon: "siren" },
  { id: "ab-sec-keys", label: "快捷键", icon: "keyboard" },
  { id: "ab-sec-mods", label: "九个模块", icon: "layers" },
  // ★ 2026-10-09：3D 可视化与占位表的详细说明统一收到这里
  //   （用户："占位表和对话的那些废话移除掉，放在关于吧，不要太乱"）
  { id: "ab-sec-3d", label: "3D 说明", icon: "camera" },
  { id: "ab-sec-notes", label: "安全须知", icon: "shieldCheck" },
  { id: "ab-sec-info", label: "系统信息", icon: "server" },
];

function goSection(id) {
  const el = document.getElementById(id);
  if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
}

// ---------------------------------------------------------------------
// 实验前检查清单
// ---------------------------------------------------------------------
const CL_KEY = "efort.about.checklist.v1";

/** { [itemId]: true } —— 只记"已确认"，未确认不出现在对象里。 */
const checked = reactive({});

function loadChecklist() {
  try {
    const raw = localStorage.getItem(CL_KEY);
    if (!raw) return;
    const obj = JSON.parse(raw);
    if (obj && typeof obj === "object") {
      Object.keys(obj).forEach((k) => { if (obj[k]) checked[k] = true; });
    }
  } catch (e) {
    /* 隐私模式 / 配额满：不持久化也不影响使用 */
  }
}

function saveChecklist() {
  try {
    localStorage.setItem(CL_KEY, JSON.stringify(checked));
  } catch (e) {
    /* 同上 */
  }
}

function toggleItem(id) {
  if (checked[id]) delete checked[id];
  else checked[id] = true;
  saveChecklist();
}

function resetChecklist() {
  Object.keys(checked).forEach((k) => delete checked[k]);
  saveChecklist();
}

/** 全量项 / 关键项 的完成度。关键项没做完就不该开工。 */
const clStat = computed(() => {
  let total = 0, done = 0, cTotal = 0, cDone = 0;
  CHECKLIST.forEach((g) => g.items.forEach((it) => {
    total += 1;
    if (checked[it.id]) done += 1;
    if (it.critical) {
      cTotal += 1;
      if (checked[it.id]) cDone += 1;
    }
  }));
  return { total, done, cTotal, cDone, critOk: cDone === cTotal, pct: total ? Math.round((done / total) * 100) : 0 };
});

// ---------------------------------------------------------------------
// 标准操作流程（手风琴）
// ---------------------------------------------------------------------
const sopOpen = reactive({ boot: true });

function toggleSop(id) {
  sopOpen[id] = !sopOpen[id];
}

const sopAllOpen = computed(() => PROCEDURES.every((p) => sopOpen[p.id]));

function toggleAllSop() {
  const next = !sopAllOpen.value;
  PROCEDURES.forEach((p) => { sopOpen[p.id] = next; });
}

// ---------------------------------------------------------------------
// 异常处置速查（搜索）
// ---------------------------------------------------------------------
const tq = ref("");

const troubleRows = computed(() => {
  const q = tq.value.trim().toLowerCase();
  if (!q) return TROUBLESHOOT.map((r, i) => ({ ...r, _i: i }));
  return TROUBLESHOOT
    .map((r, i) => ({ ...r, _i: i }))
    .filter((r) => `${r.symptom} ${r.cause} ${r.action}`.toLowerCase().includes(q));
});

// ---------------------------------------------------------------------
// 复制
// ---------------------------------------------------------------------
const copiedKind = ref("");

async function copyText(txt, kind) {
  try {
    await navigator.clipboard.writeText(txt);
    copiedKind.value = kind;
    setTimeout(() => { if (copiedKind.value === kind) copiedKind.value = ""; }, 1600);
  } catch (e) {
    copiedKind.value = "";
  }
}

/** 整页说明：快速上手 + 检查清单 + SOP + 注意事项 + 快捷键 + 模块。 */
function buildHelpText() {
  const L = [];
  L.push(BRAND.name);
  L.push("=".repeat(40));
  L.push(`设备：${BRAND.device}    缩写：${BRAND.abbr}`);
  L.push("");

  L.push("【快速上手】");
  QUICKSTART.forEach((s, i) => L.push(`${i + 1}. ${s.title} —— ${plain(s.desc)}`));
  L.push("");

  L.push("【实验前检查清单】（★ = 不满足就不要开始实验）");
  CHECKLIST.forEach((g) => {
    L.push(`-- ${g.group} --`);
    g.items.forEach((it) => L.push(`  [ ] ${it.critical ? "★ " : ""}${plain(it.label)}`));
  });
  L.push("");

  L.push("【标准操作流程】");
  PROCEDURES.forEach((p) => {
    L.push(`-- ${p.title} --`);
    L.push(`  为什么：${plain(p.why)}`);
    p.steps.forEach((s, i) => {
      L.push(`  ${i + 1}) ${s.warn ? "⚠ " : ""}${plain(s.text)}`);
      if (s.note) L.push(`        ↳ ${plain(s.note)}`);
    });
  });
  L.push("");

  L.push("【注意事项】");
  PRECAUTIONS.forEach((g) => {
    L.push(`-- ${g.group} --`);
    g.items.forEach((it) => L.push(`  · [${it.level}] ${it.title}：${plain(it.text)}`));
  });
  L.push("");

  L.push("【异常处置速查】");
  TROUBLESHOOT.forEach((r) => {
    L.push(`  现象：${plain(r.symptom)}`);
    L.push(`  原因：${plain(r.cause)}`);
    L.push(`  处置：${plain(r.action)}`);
  });
  L.push("");

  L.push("【键盘快捷键】");
  rows.value.forEach((g) => {
    L.push(`-- ${g.group} --`);
    g.items.forEach((s) => L.push(`  ${s.label}\t${s.desc}`));
  });
  L.push("");

  L.push("【模块】");
  modules.value.forEach((m) => L.push(`${m.label}：${plain(m.desc)}`));
  return L.join("\n");
}

function copyHelp() {
  return copyText(buildHelpText(), "help");
}

/** 只复制当前未确认的检查项，方便贴在现场白板上。 */
function copyChecklistTodo() {
  const L = ["待确认项 —— 上机前逐条打勾"];
  CHECKLIST.forEach((g) => {
    const todo = g.items.filter((it) => !checked[it.id]);
    if (!todo.length) return;
    L.push(`-- ${g.group} --`);
    todo.forEach((it) => L.push(`  [ ] ${it.critical ? "★ " : ""}${plain(it.label)}`));
  });
  return copyText(L.join("\n"), "todo");
}

/** Esc / Enter / 空格 在快捷键表里显示成符号更好认。 */
const KEY_LABEL = { escape: "Esc", enter: "Enter", " ": "空格" };
function keyText(s) {
  return s.label || s.keys.map((k) => KEY_LABEL[k] || k.toUpperCase()).join(" / ");
}

/** 注意事项的左侧色条按 level 走，别用 || 兜底（缺配置会全变一个色）。 */
const PREC_STYLE = {
  danger: { cls: "danger", icon: "alert" },
  warn: { cls: "warn", icon: "alert" },
  info: { cls: "info", icon: "info" },
};
function precStyle(level) {
  return PREC_STYLE[level] || PREC_STYLE.info;
}

onMounted(() => {
  settings.loadLive();
  loadChecklist();
});
</script>

<template>
  <div class="about">
    <!-- ===== 品牌抬头 ===== -->
    <div class="ab-hero">
      <span class="ab-mark"><Icon :name="BRAND.logo" :size="34" /></span>
      <div class="ab-id">
        <div class="ab-name">{{ BRAND.name }}</div>
        <div class="ab-en">{{ BRAND.en }}</div>
        <div class="ab-tags">
          <span class="ab-tag">设备 {{ BRAND.device }}</span>
          <span class="ab-tag" v-if="live">服务 v{{ live.service.version }}</span>
          <span class="ab-tag" v-if="live">Python {{ live.service.python }}</span>
          <span class="ab-tag" :class="auth.controlActive ? 'ok' : 'warn'">
            {{ auth.controlActive ? `已授权 · ${auth.roleLabel || auth.role}` : "未授权（只读）" }}
          </span>
        </div>
      </div>
      <button class="ab-btn" @click="copyHelp">
        <Icon :name="copiedKind === 'help' ? 'check' : 'file'" :size="14" />
        {{ copiedKind === "help" ? "已复制" : "复制整页说明" }}
      </button>
    </div>

    <!-- ===== 章节导航 ===== -->
    <div class="ab-nav">
      <button v-for="s in SECTIONS" :key="s.id" class="ab-nav-chip" @click="goSection(s.id)">
        <Icon :name="s.icon" :size="12" /> {{ s.label }}
      </button>
    </div>

    <!-- ===== 快速上手 ===== -->
    <div class="ab-sec" id="ab-sec-quick">
      <div class="ab-head">
        <Icon name="flask" :size="16" />
        <span class="ab-title">快速上手</span>
        <span class="ab-sub">按顺序做完这几步就能干活</span>
      </div>
      <ol class="ab-steps">
        <li v-for="(s, i) in QUICKSTART" :key="i">
          <div class="ab-step-h"><b>{{ s.title }}</b></div>
          <div class="ab-step-d">{{ plain(s.desc) }}</div>
          <div class="ab-step-x">{{ plain(s.detail) }}</div>
        </li>
      </ol>
    </div>

    <!-- ===== 实验前检查清单 ===== -->
    <div class="ab-sec" id="ab-sec-check">
      <div class="ab-head">
        <Icon name="check" :size="16" />
        <span class="ab-title">实验前检查清单</span>
        <span class="ab-sub">★ 关键项全部确认前不要开始实验</span>
        <div class="ab-head-act">
          <button class="ab-btn sm" @click="copyChecklistTodo">
            <Icon :name="copiedKind === 'todo' ? 'check' : 'file'" :size="12" />
            {{ copiedKind === "todo" ? "已复制" : "复制待确认项" }}
          </button>
          <button class="ab-btn sm" @click="resetChecklist">
            <Icon name="refresh" :size="12" /> 重置勾选
          </button>
        </div>
      </div>

      <div class="ab-prog" :class="{ ok: clStat.critOk }">
        <div class="ab-prog-bar"><i :style="{ width: clStat.pct + '%' }"></i></div>
        <div class="ab-prog-txt">
          已确认 <b>{{ clStat.done }}</b> / {{ clStat.total }} 项（{{ clStat.pct }}%）
          <span class="ab-prog-sep">·</span>
          关键项 <b>{{ clStat.cDone }}</b> / {{ clStat.cTotal }} 项
          <span v-if="clStat.critOk" class="ab-prog-ok"><Icon name="check" :size="12" /> 关键项已全部确认，可以开工</span>
          <span v-else class="ab-prog-warn"><Icon name="alert" :size="12" /> 还有 {{ clStat.cTotal - clStat.cDone }} 项关键条件未确认</span>
        </div>
      </div>

      <div class="ab-cl-groups">
        <div v-for="g in CHECKLIST" :key="g.id" class="ab-cl-group">
          <div class="ab-cl-gh">
            <Icon :name="g.icon" :size="14" /> <b>{{ g.group }}</b>
            <span class="ab-cl-cnt">{{ g.items.filter((it) => checked[it.id]).length }}/{{ g.items.length }}</span>
          </div>
          <label v-for="it in g.items" :key="it.id" class="ab-cl-item"
                 :class="{ done: checked[it.id], critical: it.critical }">
            <input type="checkbox" :checked="!!checked[it.id]" @change="toggleItem(it.id)" />
            <span class="ab-cl-body">
              <span class="ab-cl-label">
                <b v-if="it.critical" class="ab-cl-star">★</b>{{ it.label }}
              </span>
              <span class="ab-cl-detail">{{ plain(it.detail) }}</span>
            </span>
          </label>
        </div>
      </div>
    </div>

    <!-- ===== 标准操作流程 ===== -->
    <div class="ab-sec" id="ab-sec-sop">
      <div class="ab-head">
        <Icon name="play" :size="16" />
        <span class="ab-title">标准操作流程</span>
        <span class="ab-sub">每一步都写了"为什么"，⚠ 是出错会出事的那一步</span>
        <div class="ab-head-act">
          <button class="ab-btn sm" @click="toggleAllSop">
            <Icon :name="sopAllOpen ? 'chevronUp' : 'chevronDown'" :size="12" />
            {{ sopAllOpen ? "全部收起" : "全部展开" }}
          </button>
        </div>
      </div>

      <div class="ab-sop">
        <div v-for="p in PROCEDURES" :key="p.id" class="ab-sop-item" :class="{ open: sopOpen[p.id] }">
          <button class="ab-sop-h" @click="toggleSop(p.id)">
            <Icon :name="sopOpen[p.id] ? 'chevronDown' : 'chevronRight'" :size="14" />
            <b>{{ p.title }}</b>
            <span v-if="p.needAuth" class="ab-sop-auth"><Icon name="lock" :size="11" /> 需授权</span>
            <span class="ab-sop-cnt">{{ p.steps.length }} 步</span>
          </button>
          <div v-show="sopOpen[p.id]" class="ab-sop-body">
            <p class="ab-sop-why">{{ plain(p.why) }}</p>
            <ol class="ab-sop-steps">
              <li v-for="(s, i) in p.steps" :key="i" :class="{ warn: s.warn }">
                <span class="ab-sop-txt">
                  <Icon v-if="s.warn" name="alert" :size="12" />{{ plain(s.text) }}
                </span>
                <span v-if="s.note" class="ab-sop-note">{{ plain(s.note) }}</span>
              </li>
            </ol>
          </div>
        </div>
      </div>
    </div>

    <!-- ===== 注意事项 ===== -->
    <div class="ab-sec" id="ab-sec-prec">
      <div class="ab-head">
        <Icon name="alert" :size="16" />
        <span class="ab-title">注意事项</span>
        <span class="ab-sub">按影响面分组；红色=会伤人/伤设备，黄色=会做错，蓝色=精度与习惯</span>
      </div>
      <div class="ab-prec-groups">
        <div v-for="g in PRECAUTIONS" :key="g.group" class="ab-prec-group">
          <div class="ab-prec-gh"><Icon :name="g.icon" :size="14" /> <b>{{ g.group }}</b></div>
          <div v-for="(it, i) in g.items" :key="i" class="ab-prec-item" :class="precStyle(it.level).cls">
            <div class="ab-prec-h">
              <Icon :name="precStyle(it.level).icon" :size="13" /><b>{{ it.title }}</b>
              <span class="ab-prec-lv">{{ it.level }}</span>
            </div>
            <p>{{ plain(it.text) }}</p>
          </div>
        </div>
      </div>
    </div>

    <!-- ===== 异常处置速查 ===== -->
    <div class="ab-sec" id="ab-sec-trouble">
      <div class="ab-head">
        <Icon name="siren" :size="16" />
        <span class="ab-title">异常处置速查</span>
        <span class="ab-sub">先看现象，再对原因，最后照处置做</span>
        <div class="ab-head-act">
          <input v-model="tq" class="ab-search" type="text" data-keys="off"
                 placeholder="搜现象 / 原因 / 处置…" />
        </div>
      </div>
      <div class="ab-tr">
        <div class="ab-tr-head">
          <span>现象</span><span>可能原因</span><span>处置</span>
        </div>
        <div v-for="r in troubleRows" :key="r._i" class="ab-tr-row">
          <span class="ab-tr-s">{{ r.symptom }}</span>
          <span class="ab-tr-c">{{ r.cause }}</span>
          <span class="ab-tr-a">{{ r.action }}</span>
        </div>
        <div v-if="!troubleRows.length" class="ab-tr-empty">没有匹配项 —— 换个关键词，或直接看「注意事项」。</div>
      </div>
      <p class="ab-note">
        ★ 界面对大多数异常都会给出<b>不同的提示文字</b>，读提示比猜快。
        真出现表格里没有的现象：先把「运维审计 → 事件时间线」的对应时段导出，再找维护人员，
        别一边报故障一边继续点动。
      </p>
    </div>

    <!-- ===== 键盘快捷键 ===== -->
    <div class="ab-sec" id="ab-sec-keys">
      <div class="ab-head">
        <Icon name="keyboard" :size="16" />
        <span class="ab-title">键盘快捷键</span>
        <span class="ab-sub">在输入框里打字时全部失效，不会误触</span>
      </div>
      <div class="ab-keys">
        <div v-for="g in rows" :key="g.group" class="ab-keygroup">
          <div class="ab-keygroup-h">{{ g.group }}</div>
          <div v-for="s in g.items" :key="s.id" class="ab-keyrow" :class="{ danger: s.danger }">
            <span class="ab-cap">{{ keyText(s) }}</span>
            <span class="ab-kdesc">
              {{ s.desc }}
              <em class="ab-kwhen">{{ s.when }}</em>
            </span>
            <span v-if="s.danger" class="ab-kflag"><Icon name="alert" :size="11" /> 危险</span>
            <span v-else-if="s.needAuth" class="ab-kflag need"><Icon name="lock" :size="11" /> 需授权</span>
          </div>
        </div>
      </div>
      <p class="ab-note">
        ★ 弹层（登录框 / 状态灯面板）打开时，<b>Esc 先关弹层</b>，再按一次才是急停 ——
        避免"想关个窗口却把机器人拍停了"。组合键（Ctrl / Alt / Cmd 同时按）本系统一概不抢。
      </p>
    </div>

    <!-- ===== 模块说明 ===== -->
    <div class="ab-sec" id="ab-sec-mods">
      <div class="ab-head">
        <Icon name="layers" :size="16" />
        <span class="ab-title">九个模块（顶栏 8 个 + 程序执行并入点位执行）</span>
        <span class="ab-sub">1 ~ 8 从左到右直达</span>
      </div>
      <div class="ab-mods">
        <div v-for="m in modules" :key="m.key" class="ab-mod"
             :class="{ locked: !enabledKeys.includes(m.key) }">
          <div class="ab-mod-h">
            <!-- ★ 编号按顶栏顺序（hidden 的「程序执行」不占数字位，显示 — 并单独标注入口） -->
            <span class="ab-mod-no">{{ modNo(m.key) || "—" }}</span>
            <Icon :name="m.icon" :size="15" />
            <b>{{ m.label }}</b>
            <span v-if="modNo(m.key) === 0" class="ab-mod-entry">入口在「点位执行」页内</span>
            <span v-if="!enabledKeys.includes(m.key)" class="ab-mod-lock">
              <Icon name="lock" :size="11" /> 需{{ m.key === 'ops' ? '管理员' : '控制' }}权限
            </span>
          </div>
          <div class="ab-mod-d">{{ plain(m.desc) }}</div>
          <div class="ab-mod-u"><span class="ab-k">什么时候用</span>{{ plain(m.use) }}</div>
          <ul class="ab-mod-t">
            <li v-for="(t, j) in m.tips" :key="j">{{ plain(t) }}</li>
          </ul>
        </div>
      </div>
    </div>

    <!-- ===== 安全须知 ===== -->
    <!-- ★ 2026-10-09 新增：把原本散在各页的长说明集中到这里，
         各页只留一行指路（用户口径："放在关于吧，不要太乱"） -->
    <div class="ab-sec" id="ab-sec-3d">
      <div class="ab-head">
        <Icon name="camera" :size="16" />
        <span class="ab-title">3D 可视化说明</span>
        <span class="ab-sub">画面里画的是什么、以及哪些是"推断"</span>
      </div>
      <div class="ab-notes">
        <div class="ab-note-item info">
          <div class="ab-note-h"><Icon name="layers" :size="14" /><b>三区构成</b></div>
          <p>
            <b>A 区</b>＝一个托盘盒里 9 块料；<b>B 区</b>＝一个托盘盒里 9 个空位；
            <b>C 区</b>＝摆在台面上的摄像头（镜头朝上）。料与托盘<b>底面坐在站台台面上</b>，
            料坐在托盘内底面上（比台面高一个托板厚度）—— 不漂浮、不悬空。
          </p>
        </div>
        <div class="ab-note-item info">
          <div class="ab-note-h"><Icon name="target" :size="14" /><b>X/Y 从哪来</b></div>
          <p>
            由<b>示教点位</b>经正运动学（FK）算出，<b>按点位示意，不是视觉定位</b> ——
            相机只对着 C 区，照不到 A/B 区。
          </p>
        </div>
        <div class="ab-note-item warn">
          <div class="ab-note-h"><Icon name="alert" :size="14" /><b>高度为什么不取示教点的 Z</b></div>
          <p>
            示教点存的是关节角，算出来的是<b>法兰中心</b>（工具安装面），料在其下方。
            直接拿它当料高会让料<b>悬空</b>（曾因此浮在台面上方 15cm）。
            所以摆放高度统一用"站台台面"，示教点的 Z 只用于共面性体检。
          </p>
        </div>
        <div class="ab-note-item info">
          <div class="ab-note-h"><Icon name="activity" :size="14" /><b>随执行进度同步</b></div>
          <p>
            A 格取走 → 消失；判色后手上那块变色；落入 B 格 → 出现同色实心块。
            <b>空跑</b>时方块只是示意、机器人不动（界面有空跑角标）。
          </p>
        </div>
        <div class="ab-note-item warn">
          <div class="ab-note-h"><Icon name="info" :size="14" /><b>哪些是"推断"，不是实测</b></div>
          <p>
            现场<b>没有真空反馈</b>：手上是否夹着件、件有没有吸住，都是<b>按流程推断</b>的。
            失败停止时会把方块画回原格并标红（"疑似没取到"），人工停止则如实显示"还夹着"。
          </p>
        </div>
        <div class="ab-note-item info">
          <div class="ab-note-h"><Icon name="layers" :size="14" /><b>「占位表」（原在 AI 执行页）</b></div>
          <p>
            那是 AI 执行页自己的记账：它<b>不共享分拣页那一轮的占位</b>，
            也<b>不知道 A/B 区真实里面有什么</b>（相机只对 C 区）——
            默认 A 区有料、B 区为空，每成功搬一件才更新。要"放到红列"就得先知道哪格空着，
            所以那张表是必需的；但它<b>不是现场实况</b>，格子里到底有什么仍需人工确认。
          </p>
        </div>
        <div class="ab-note-item warn">
          <div class="ab-note-h"><Icon name="camera" :size="14" /><b>判色阈值（矩形填充率 / 置信度）</b></div>
          <p>
            「矩形填充率」是「只识别矩形」的硬门槛：低于此值直接判非矩形、不输出颜色（0 = 关闭）。
            它必须<b>低于现场实测最低填充率</b>（真矩形实测 0.67~0.75）—— 定太高会误杀真方块，
            表现为「三色全都检测不到」（2026-10-08 事故：曾定为 0.72 → 绿色/蓝色全部被判非矩形）。
            阈值与旋转无关；被遮挡较厉害的件会被判非矩形 → 分拣按「检测不到」停，觉得太严就调低。
            改阈值前请先跑现场真图回放（camera/tools/vision_e2e_replay.py），不要按合成图定。
          </p>
        </div>
      </div>
    </div>

    <div class="ab-sec" id="ab-sec-notes">
      <div class="ab-head">
        <Icon name="shieldCheck" :size="16" />
        <span class="ab-title">安全须知</span>
        <span class="ab-sub">这些是代码里真实存在的约束，不是泛泛的提醒</span>
      </div>
      <div class="ab-notes">
        <div v-for="(n, i) in SAFETY_NOTES" :key="i" class="ab-note-item" :class="n.level">
          <div class="ab-note-h">
            <Icon :name="n.level === 'info' ? 'info' : 'alert'" :size="14" />
            <b>{{ n.title }}</b>
          </div>
          <p>{{ plain(n.text) }}</p>
        </div>
      </div>
    </div>

    <!-- ===== 系统信息 ===== -->
    <div class="ab-sec" id="ab-sec-info">
      <div class="ab-head">
        <Icon name="server" :size="16" />
        <span class="ab-title">系统信息</span>
        <span class="ab-sub">排查时经常要抄给别人的那几行</span>
      </div>
      <div class="ab-info">
        <div class="ab-row"><span class="ab-k">项目</span><span class="ab-v">{{ BRAND.name }}</span></div>
        <div class="ab-row"><span class="ab-k">缩写</span><span class="ab-v">{{ BRAND.abbr }}</span></div>
        <div class="ab-row"><span class="ab-k">设备</span><span class="ab-v">{{ BRAND.device }}</span></div>
        <template v-if="live">
          <div class="ab-row"><span class="ab-k">服务</span>
            <span class="ab-v">v{{ live.service.version }} · {{ live.service.host }}:{{ live.service.port }}
              · 已运行 {{ live.service.uptime_sec }}s</span></div>
          <div class="ab-row"><span class="ab-k">WebSocket</span><span class="ab-v">{{ live.service.ws_path }}</span></div>
          <div class="ab-row"><span class="ab-k">机器人</span>
            <span class="ab-v">{{ live.robot.model }} · {{ live.robot.serial }} · {{ live.robot.host }}:{{ live.robot.port }}
              · <span :class="live.robot.level === 'ok' ? 'ab-ok' : 'ab-warn'">{{ live.robot.label }}</span></span></div>
          <div class="ab-row"><span class="ab-k">相机服务</span>
            <span class="ab-v">{{ live.camera.base }} · <span :class="live.camera.level === 'ok' ? 'ab-ok' : 'ab-warn'">{{ live.camera.label }}</span></span></div>
          <div class="ab-row"><span class="ab-k">数据库</span>
            <span class="ab-v">{{ live.database.ok ? `${live.database.size_kb} KB · ${live.database.path}` : "不可用" }}</span></div>
          <div class="ab-row"><span class="ab-k">配置文件</span><span class="ab-v">{{ live.paths.config }}</span></div>
          <div class="ab-row"><span class="ab-k">覆盖层</span><span class="ab-v">{{ live.paths.settings }}</span></div>
          <div class="ab-row"><span class="ab-k">真实下发</span>
            <span class="ab-v" :class="live.runtime.real_motion_active ? 'ab-err' : 'ab-ok'">
              {{ live.runtime.real_motion_active ? "★ 已启用（会真正写控制器）" : "未启用（安全模拟）" }}
              <em class="ab-em">env={{ live.runtime.real_motion_env ? "1" : "0" }} · real_write={{ live.runtime.real_write ? "true" : "false" }}</em>
            </span></div>
        </template>
        <div class="ab-row"><span class="ab-k">链路</span>
          <span class="ab-v">
            机器人 {{ link.robot ? link.robot.label : "未知" }} ·
            摄像头 {{ link.camera ? link.camera.label : "未知" }} ·
            示教器 {{ link.runMode ? link.runMode.label : "未知" }}
          </span></div>
      </div>
      <div class="ab-links">
        <a class="ab-link" :href="'/docs'" target="_blank" rel="noopener">
          <Icon name="externalLink" :size="13" /> 接口文档 /docs
        </a>
        <a class="ab-link" :href="'/healthz'" target="_blank" rel="noopener">
          <Icon name="externalLink" :size="13" /> 存活探针 /healthz
        </a>
        <a class="ab-link" :href="'/api/version'" target="_blank" rel="noopener">
          <Icon name="externalLink" :size="13" /> 版本 /api/version
        </a>
      </div>
    </div>
  </div>
</template>

<style scoped>
.about { flex: 1; min-width: 0; min-height: 0; overflow-y: auto; padding: 10px;
  display: flex; flex-direction: column; gap: 10px; align-items: center; }
/* ★ 需求：关于页内容宽度 1200px 并水平居中 */
.about > * { width: 100%; max-width: 1200px; }

/* ---- 品牌抬头 ---- */
.ab-hero { display: flex; align-items: center; gap: 10px; padding: 10px;
  border: 1px solid var(--line); border-radius: 10px; background: var(--panel); }
.ab-mark { flex: none; width: 58px; height: 58px; display: grid; place-items: center;
  border-radius: 14px; color: var(--accent);
  background: var(--accent-soft); border: 1px solid var(--accent-line); }
.ab-id { min-width: 0; flex: 1; }
.ab-name { font-size: 19px; font-weight: 700; color: var(--txt); letter-spacing: .3px; }
.ab-en { font-size: 11px; color: var(--muted); margin-top: 3px; }
.ab-tags { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 9px; }
.ab-tag { font-size: 11px; padding: 3px 8px; border-radius: 20px;
  border: 1px solid var(--line); background: var(--panel2); color: var(--muted); }
.ab-tag.ok { color: var(--ok); border-color: var(--ok-line); }
.ab-tag.warn { color: var(--warn); border-color: var(--warn-line); }
.ab-btn { flex: none; align-self: flex-start; display: inline-flex; align-items: center; gap: 5px;
  padding: 7px 10px; font-size: 12px; }
.ab-btn.sm { padding: 4px 9px; font-size: 11px; }

/* ---- 章节导航 ---- */
/* ★ St2：sticky top 从 -14px 改为 0（容器 .about 有 padding:10px，负吸附点会盖住上一卡底边） */
.ab-nav { display: flex; flex-wrap: wrap; gap: 6px; padding: 9px 10px;
  border: 1px solid var(--line); border-radius: 10px; background: var(--panel);
  position: sticky; top: 0; z-index: 3; backdrop-filter: blur(6px); }
.ab-nav-chip { display: inline-flex; align-items: center; gap: 4px; padding: 4px 9px;
  font-size: 11px; border-radius: 20px; color: var(--muted);
  border: 1px solid var(--line); background: var(--panel2); cursor: pointer; }
.ab-nav-chip:hover { color: var(--accent); border-color: var(--accent); }
.ab-nav-chip svg { color: var(--accent); }

/* ---- 通用分节 ---- */
.ab-sec { border: 1px solid var(--line); border-radius: 10px; background: var(--panel); padding: 10px;
  scroll-margin-top: 46px; }
.ab-head { display: flex; align-items: center; gap: 7px; flex-wrap: wrap; }
.ab-head svg { color: var(--accent); }
.ab-title { font-size: 14px; font-weight: 700; color: var(--txt); }
.ab-sub { font-size: 11px; color: var(--muted); margin-left: 4px; }
.ab-head-act { margin-left: auto; display: inline-flex; align-items: center; gap: 6px; }

/* ---- 快速上手 ---- */
.ab-steps { margin: 0; padding-left: 10px; display: flex; flex-direction: column; gap: 5px; }
.ab-steps li { color: var(--txt); }
.ab-step-h { font-size: 13px; color: var(--txt); }
.ab-step-d { font-size: 12px; color: var(--muted); margin-top: 4px; line-height: 1.65; }
.ab-step-x { font-size: 11px; color: var(--muted); margin-top: 4px; line-height: 1.7;
  padding-left: 9px; border-left: 2px solid var(--line); }

/* ---- 检查清单 ---- */
.ab-prog-bar { height: 6px; border-radius: 4px; overflow: hidden;
  background: var(--bg); border: 1px solid var(--line); }
.ab-prog-bar i { display: block; height: 100%; background: var(--warn); transition: width .22s ease; }
.ab-prog.ok .ab-prog-bar i { background: var(--ok); }
.ab-prog-txt { margin-top: 6px; font-size: 11px; color: var(--muted);
  display: flex; align-items: center; flex-wrap: wrap; gap: 5px; }
.ab-prog-txt b { color: var(--txt); font-variant-numeric: tabular-nums; }
.ab-prog-sep { color: var(--line); }
.ab-prog-ok { display: inline-flex; align-items: center; gap: 4px; color: var(--ok); }
.ab-prog-warn { display: inline-flex; align-items: center; gap: 4px; color: var(--warn); }

.ab-cl-groups { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 10px; }
.ab-cl-group { border: 1px solid var(--line); border-radius: 8px; background: var(--panel2); padding: 9px; }
.ab-cl-gh { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--txt);
  padding: 0 0 7px; border-bottom: 1px solid var(--line); }
.ab-cl-gh svg { color: var(--accent); }
.ab-cl-cnt { margin-left: auto; font-size: 11px; color: var(--muted); font-variant-numeric: tabular-nums; }
.ab-cl-item { display: flex; align-items: flex-start; gap: 8px; padding: 6px 7px; border-radius: 6px;
  cursor: pointer; border: 1px solid transparent; }
.ab-cl-item:hover { background: var(--panel); }
.ab-cl-item.critical { border-left: 2px solid var(--err-line); }
.ab-cl-item.done { opacity: .62; }
.ab-cl-item.done .ab-cl-label { text-decoration: line-through; }
.ab-cl-item input { flex: none; margin-top: 2px; accent-color: var(--accent); }
.ab-cl-body { min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.ab-cl-label { font-size: 12px; color: var(--txt); line-height: 1.5; }
.ab-cl-star { color: var(--err); margin-right: 3px; }
.ab-cl-detail { font-size: 11px; color: var(--muted); line-height: 1.65; }

/* ---- SOP ---- */
.ab-sop { display: flex; flex-direction: column; gap: 7px; }
.ab-sop-item { border: 1px solid var(--line); border-radius: 8px; background: var(--panel2); }
.ab-sop-item.open { border-color: var(--accent-line); }
.ab-sop-h { width: 100%; display: flex; align-items: center; gap: 7px; padding: 10px;
  background: none; border: none; cursor: pointer; text-align: left; }
.ab-sop-h b { font-size: 13px; color: var(--txt); }
.ab-sop-h svg { color: var(--accent); flex: none; }
.ab-sop-auth { display: inline-flex; align-items: center; gap: 3px; font-size: 10px; color: var(--warn); }
.ab-sop-cnt { margin-left: auto; font-size: 11px; color: var(--muted); }
.ab-sop-body { padding: 0 10px 10px; }
.ab-sop-why { font-size: 11px; color: var(--muted); line-height: 1.75;
  padding: 7px 9px; border-radius: 6px; background: var(--panel); border-left: 2px solid var(--accent); }
.ab-sop-steps { margin: 0; padding-left: 10px; display: flex; flex-direction: column; gap: 5px; }
.ab-sop-steps li { font-size: 12px; color: var(--txt); line-height: 1.6; }
.ab-sop-steps li.warn { color: var(--err); }
.ab-sop-txt { display: inline-flex; align-items: flex-start; gap: 4px; }
.ab-sop-txt svg { flex: none; margin-top: 3px; }
.ab-sop-note { display: block; margin-top: 3px; font-size: 11px; color: var(--muted); line-height: 1.7;
  padding-left: 8px; border-left: 2px solid var(--line); }

/* ---- 注意事项 ---- */
.ab-prec-groups { display: grid; grid-template-columns: repeat(auto-fit, minmax(330px, 1fr)); gap: 10px; }
.ab-prec-group { display: flex; flex-direction: column; gap: 7px; }
.ab-prec-gh { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--txt); }
.ab-prec-gh svg { color: var(--accent); }
.ab-prec-item { border: 1px solid var(--line); border-left-width: 3px; border-radius: 7px;
  padding: 9px 10px; background: var(--panel2); }
.ab-prec-item.danger { border-left-color: var(--err); }
.ab-prec-item.warn { border-left-color: var(--warn); }
.ab-prec-item.info { border-left-color: var(--accent); }
.ab-prec-h { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--txt); }
.ab-prec-item.danger .ab-prec-h svg { color: var(--err); }
.ab-prec-item.warn .ab-prec-h svg { color: var(--warn); }
.ab-prec-item.info .ab-prec-h svg { color: var(--accent); }
.ab-prec-lv { margin-left: auto; font-size: 10px; color: var(--muted); letter-spacing: .5px; }
.ab-prec-item p { margin: 6px 0 0; font-size: 11px; color: var(--muted); line-height: 1.75; }

/* ---- 异常速查 ---- */
.ab-search { width: 190px; font-size: 11px; padding: 5px 9px; border-radius: 6px;
  border: 1px solid var(--line); background: var(--bg); color: var(--txt); }
.ab-tr { border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
.ab-tr-head, .ab-tr-row { display: grid; grid-template-columns: minmax(150px, 1fr) minmax(190px, 1.3fr) minmax(190px, 1.3fr);
  gap: 10px; padding: 8px 10px; }
.ab-tr-head { background: var(--panel2); font-size: 11px; color: var(--accent); letter-spacing: .4px;
  border-bottom: 1px solid var(--line); }
.ab-tr-row { font-size: 11px; line-height: 1.7; border-bottom: 1px solid var(--line); }
.ab-tr-row:last-child { border-bottom: none; }
.ab-tr-row:hover { background: var(--panel2); }
.ab-tr-s { color: var(--txt); }
.ab-tr-c { color: var(--muted); }
.ab-tr-a { color: var(--muted); }
.ab-tr-empty { padding: 10px; text-align: center; font-size: 11px; color: var(--muted); }

/* ---- 快捷键 ---- */
.ab-keys { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 10px; }
.ab-keygroup-h { font-size: 11px; color: var(--accent); letter-spacing: .5px; }
.ab-keyrow { display: flex; align-items: center; gap: 9px; padding: 6px 8px; border-radius: 7px;
  background: var(--panel2); border: 1px solid var(--line); }
.ab-keyrow.danger { border-color: var(--err-line); background: var(--err-soft); }
.ab-cap { flex: none; min-width: 54px; text-align: center; padding: 3px 7px; border-radius: 5px;
  font-size: 11px; font-weight: 700; color: var(--txt);
  background: var(--bg); border: 1px solid var(--line); box-shadow: 0 1px 0 var(--line); }
.ab-kdesc { flex: 1; min-width: 0; font-size: 12px; color: var(--txt); line-height: 1.5; }
.ab-kwhen { display: block; font-style: normal; font-size: 11px; color: var(--muted); margin-top: 2px; }
.ab-kflag { flex: none; display: inline-flex; align-items: center; gap: 3px; font-size: 10px;
  padding: 2px 6px; border-radius: 20px; color: var(--err); border: 1px solid var(--err-line); }
.ab-kflag.need { color: var(--muted); border-color: var(--line); }
.ab-note { margin: 10px 0 0; font-size: 11px; color: var(--muted); line-height: 1.7;
  padding-top: 10px; border-top: 1px solid var(--line); }
.ab-note b { color: var(--txt); }

/* ---- 模块 ---- */
.ab-mods { display: grid; grid-template-columns: repeat(auto-fit, minmax(290px, 1fr)); gap: 10px; }
.ab-mod { border: 1px solid var(--line); border-radius: 8px; padding: 10px; background: var(--panel2); }
.ab-mod.locked { opacity: .62; }
.ab-mod-h { display: flex; align-items: center; gap: 6px; font-size: 13px; color: var(--txt); }
.ab-mod-h svg { color: var(--accent); }
.ab-mod-no { flex: none; width: 17px; height: 17px; display: grid; place-items: center; border-radius: 4px;
  font-size: 10px; font-weight: 700; color: var(--accent); border: 1px solid var(--accent-line); }
.ab-mod-lock { margin-left: auto; display: inline-flex; align-items: center; gap: 3px;
  font-size: 10px; color: var(--warn); }
/* ★ hidden 模块（不在顶栏，如「程序执行」）：不占数字位，单独标注入口在哪儿 */
.ab-mod-entry { font-size: 10px; color: var(--muted); }
.ab-mod-d { font-size: 12px; color: var(--muted); margin-top: 7px; line-height: 1.6; }
.ab-mod-u { font-size: 11px; color: var(--muted); margin-top: 6px; line-height: 1.6; }
.ab-mod-t { margin: 7px 0 0; padding-left: 16px; }
.ab-mod-t li { font-size: 11px; color: var(--muted); line-height: 1.7; }

/* ---- 安全须知 ---- */
.ab-notes { display: flex; flex-direction: column; gap: 8px; }
.ab-note-item { border: 1px solid var(--line); border-left-width: 3px; border-radius: 7px;
  padding: 10px; background: var(--panel2); }
.ab-note-item.danger { border-left-color: var(--err); }
.ab-note-item.warn { border-left-color: var(--warn); }
.ab-note-item.info { border-left-color: var(--accent); }
.ab-note-h { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--txt); }
.ab-note-item.danger .ab-note-h svg { color: var(--err); }
.ab-note-item.warn .ab-note-h svg { color: var(--warn); }
.ab-note-item.info .ab-note-h svg { color: var(--accent); }
.ab-note-item p { margin: 6px 0 0; font-size: 11px; color: var(--muted); line-height: 1.75; }

/* ---- 系统信息 ---- */
.ab-info { display: flex; flex-direction: column; gap: 3px; }
.ab-row { display: flex; align-items: baseline; gap: 10px; font-size: 12px; padding: 3px 0; }
.ab-k { flex: none; width: 82px; color: var(--muted); font-size: 11px; }
.ab-v { min-width: 0; color: var(--txt); word-break: break-all; font-variant-numeric: tabular-nums; }
.ab-em { font-style: normal; color: var(--muted); font-size: 11px; margin-left: 4px; }
.ab-ok { color: var(--ok); }
.ab-warn { color: var(--warn); }
.ab-err { color: var(--err); }
.ab-links { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px;
  padding-top: 12px; border-top: 1px solid var(--line); }
.ab-link { display: inline-flex; align-items: center; gap: 5px; font-size: 12px;
  color: var(--accent); text-decoration: none; padding: 5px 10px; border-radius: 6px;
  border: 1px solid var(--line); background: var(--panel2); }
.ab-link:hover { border-color: var(--accent); }
/* ---- 窄屏 ---- */
@media (max-width: 900px) {
  .ab-tr-head, .ab-tr-row { grid-template-columns: 1fr; gap: 3px; }
  .ab-tr-head { display: none; }
  .ab-tr-s { color: var(--accent); }
}
</style>
