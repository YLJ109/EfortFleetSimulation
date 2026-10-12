﻿<script setup>
// 播报中心设置面板：分级音效 + 中文语音 的总开关、音量/语速/音色、分类静音、
// 一键试听与播报历史。
import { computed, onMounted, ref } from "vue";
import Icon from "./Icon.vue";
import { annCfg, setConfig, testAnnounce, annHistory, clearHistory, listVoices, unlockAudio, audioReady } from "../services/announcer.js";

// ★ P1-E13：只声明不接引用（模板里直接用 open，本文件没有读 props.xxx）。
defineProps({ open: { type: Boolean, default: false } });
const emit = defineEmits(["close"]);

const voices = ref([]);
const ready = ref(false);

onMounted(() => {
  voices.value = listVoices();
  ready.value = audioReady();
  setTimeout(() => { voices.value = listVoices(); }, 400);
});

const LEVELS = [
  { key: "info", label: "提示", cls: "lv-info" },
  { key: "warn", label: "预警", cls: "lv-warn" },
  { key: "error", label: "报警", cls: "lv-error" },
  { key: "critical", label: "紧急", cls: "lv-crit" },
];
const CATS = [
  { key: "vision", label: "视觉检测播报" },
];

function set(k, v) { setConfig({ [k]: v }); }
function setQuiet(k, v) { setConfig({ quiet: { ...annCfg.value.quiet, [k]: v } }); }

function onTest(level) {
  unlockAudio();
  ready.value = audioReady();
  testAnnounce(level, level === "critical" ? "紧急报警测试，请立即急停"
    : level === "error" ? "报警测试，运动已禁止"
    : level === "warn" ? "预警测试，请注意安全距离" : "播报测试，系统正常");
}

const volPct = computed(() => Math.round((annCfg.value.volume || 0) * 100));
const timeText = (d) => new Date(d).toLocaleTimeString("zh-CN", { hour12: false });
</script>

<template>
  <div v-if="open" class="ap-wrap" @click.self="emit('close')">
    <div class="ap-panel">
      <div class="ap-head">
        <Icon name="megaphone" :size="15" class="ap-ico" />
        <span class="ap-title">播报中心</span>
        <button class="ap-x" title="关闭" @click="emit('close')">
          <Icon name="close" :size="14" />
        </button>
      </div>

      <div class="ap-body">
        <p v-if="!ready" class="ap-tip">
          <Icon name="alert" :size="13" /> 浏览器需要一次点击才能启用声音，点任意「试听」即可解锁。
        </p>

        <div class="ap-row">
          <span class="k">总开关</span>
          <input type="checkbox" :checked="annCfg.enabled" @change="set('enabled', $event.target.checked)" />
          <span class="k">音效</span>
          <input type="checkbox" :checked="annCfg.sound" @change="set('sound', $event.target.checked)" />
          <span class="k">语音</span>
          <input type="checkbox" :checked="annCfg.voice" @change="set('voice', $event.target.checked)" />
        </div>

        <div class="ap-row">
          <span class="k">音量</span>
          <input type="range" min="0" max="100" :value="volPct"
                 @input="set('volume', (+$event.target.value) / 100)" />
          <span class="num">{{ volPct }}%</span>
        </div>
        <div class="ap-row">
          <span class="k">语速</span>
          <input type="range" min="0.7" max="1.4" step="0.05" :value="annCfg.rate"
                 @input="set('rate', +$event.target.value)" />
          <span class="num">{{ (annCfg.rate || 1).toFixed(2) }}×</span>
        </div>
        <div class="ap-row">
          <span class="k">音色</span>
          <select :value="annCfg.voiceURI" @change="set('voiceURI', $event.target.value)">
            <option value="">自动（中文优先）</option>
            <option v-for="v in voices" :key="v.voiceURI" :value="v.voiceURI">{{ v.name }}</option>
          </select>
        </div>

        <div class="ap-sub">分类静音（勾选＝该类不语音播报）</div>
        <div class="ap-row wrap">
          <label v-for="l in LEVELS" :key="l.key" class="ap-chip" :class="l.cls">
            <input type="checkbox" :checked="annCfg.quiet[l.key]"
                   @change="setQuiet(l.key, $event.target.checked)" />
            {{ l.label }}
          </label>
          <label v-for="c in CATS" :key="c.key" class="ap-chip">
            <input type="checkbox" :checked="annCfg.quiet[c.key]"
                   @change="setQuiet(c.key, $event.target.checked)" />
            {{ c.label }}
          </label>
        </div>
        <div class="ap-row">
          <label class="ap-line">
            <input type="checkbox" :checked="annCfg.forceCritical"
                   @change="set('forceCritical', $event.target.checked)" />
            静音时「紧急」级别仍然播报
          </label>
        </div>

        <div class="ap-sub">试听</div>
        <div class="ap-btns">
          <button v-for="l in LEVELS" :key="l.key" :class="l.cls" @click="onTest(l.key)">
            <Icon name="volume" :size="13" /> {{ l.label }}
          </button>
          <button class="lv-info" @click="onTest('vision')">
            <Icon name="palette" :size="13" /> 视觉
          </button>
        </div>

        <div class="ap-sub">
          播报历史
          <button class="ap-mini" @click="clearHistory()">清空</button>
        </div>
        <div class="ap-hist">
          <div v-for="(h, i) in annHistory" :key="i" class="ap-h" :class="'lv-' + (h.level === 'critical' ? 'crit' : h.level)">
            <span class="t">{{ timeText(h.t) }}</span>
            <span class="x">{{ h.text }}</span>
          </div>
          <p v-if="!annHistory.length" class="small">暂无播报记录</p>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.ap-wrap { position: fixed; inset: 0; z-index: 150; background: var(--scrim); }
.ap-panel { position: absolute; right: 16px; top: 54px; width: 380px; max-height: calc(100vh - 76px);
  display: flex; flex-direction: column; background: var(--panel); border: 1px solid var(--line);
  border-radius: 10px; box-shadow: var(--shadow-lg); overflow: hidden; }
.ap-head { display: flex; align-items: center; gap: 7px; padding: 9px 10px; font-size: 13px;
  border-bottom: 1px solid var(--line); background: linear-gradient(180deg, var(--veil), transparent); }
.ap-ico { color: var(--accent); }
.ap-title { font-weight: 600; }
.ap-x { margin-left: auto; width: 24px; height: 24px; padding: 0; display: inline-flex;
  align-items: center; justify-content: center; border: none; background: transparent;
  color: var(--muted); cursor: pointer; border-radius: 6px; }
.ap-x:hover { background: var(--veil2); color: var(--txt); }
.ap-body { padding: 10px; overflow: auto; display: flex; flex-direction: column; gap: 5px; }
.ap-row { display: flex; align-items: center; gap: 8px; font-size: 12px; }
.ap-row.wrap { flex-wrap: wrap; }
.ap-row .k { color: var(--muted); flex: none; }
.ap-row .num { color: var(--txt); width: 46px; text-align: right; font-variant-numeric: tabular-nums; }
.ap-row input[type=range] { flex: 1; accent-color: var(--accent); }
.ap-row select { flex: 1; min-width: 0; background: var(--bg); color: var(--txt);
  border: 1px solid var(--line); border-radius: 4px; padding: 4px 6px; font-size: 12px; }
.ap-row input[type=checkbox] { accent-color: var(--accent); }
.ap-sub { color: var(--muted); font-size: 11px; margin-top: 4px; display: flex; align-items: center; gap: 8px; }
.ap-mini { margin-left: auto; font-size: 11px; padding: 1px 6px; }
.ap-chip { display: inline-flex; align-items: center; gap: 4px; font-size: 11px; padding: 2px 7px;
  border: 1px solid var(--line); border-radius: 10px; background: var(--panel2); }
.ap-line { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--txt); }
.ap-btns { display: flex; gap: 6px; flex-wrap: wrap; }
.ap-btns button { flex: 1; display: inline-flex; align-items: center; justify-content: center;
  gap: 5px; padding: 5px 8px; font-size: 12px; min-width: 78px; }
.lv-info { color: var(--accent); }
.lv-warn { color: var(--warn); }
.lv-error { color: var(--err-fg); }
.lv-crit { color: var(--err); }
.ap-hist { display: flex; flex-direction: column; gap: 4px; max-height: 150px; overflow: auto;
  border: 1px solid var(--line); border-radius: 6px; padding: 6px; background: var(--panel2); }
.ap-h { display: flex; gap: 8px; font-size: 11px; }
.ap-h .t { color: var(--muted); flex: none; font-variant-numeric: tabular-nums; }
.ap-h .x { flex: 1; min-width: 0; word-break: break-all; }
.ap-tip { color: var(--warn); font-size: 11px; display: flex; align-items: center; gap: 5px; }
.small { font-size: 11px; color: var(--muted); }
</style>
