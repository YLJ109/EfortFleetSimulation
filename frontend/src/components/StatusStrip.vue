<script setup>
// =====================================================================
// 底栏常驻状态灯：机器人链路 / 摄像头 / 示教器档位。
//
// 三条硬性要求（来自现场）：
//   1. **任何情况下三盏灯都在**，不是出问题才出现 —— 现场要的是"一眼看全"，
//      而不是"没报警就以为都好"。所以 links 缺失时显示灰灯"未知"，不是隐藏。
//   2. 示教器档位**不管有没有 AUTO 都要显示**。档位优先由控制器状态字**实测**
//      （AUTO/远程自动确认，不用人工声明）；控制器读不到时才回落到人工声明。
//      所以"未确认"本身就是有效信息 —— 如实显示，而不是因为没有数据就不显示。
//   3. 点灯能展开详情 + 就地重连/声明，不用再跳页面找按钮。
//
// 状态 → 外观的映射全在 src/linkview.js（纯函数，可被 node 断言）。
// 数据来自 stores/link.js（唯一的 /system/guide 轮询者）。
// =====================================================================
import { computed } from "vue";
import Icon from "./Icon.vue";
import { useLinkStore } from "../stores/link.js";
import { useCameraStore } from "../stores/camera.js";
import { useUiStore } from "../stores/ui.js";

const emit = defineEmits(["request-control"]);

const link = useLinkStore();
const cam = useCameraStore();
const ui = useUiStore();

/**
 * 当前展开的灯（"" = 全部收起）。三盏灯共用一个展开位，避免并排弹出挡住视口。
 * ★ 状态放在 ui store 而不是局部 ref：全局键盘处理要能知道"现在有浮层开着"，
 *   这样按 Esc 才是"关浮层"而不是"急停"（见 src/shortcuts.js 的 resolveKey）。
 */
const open = computed(() => ui.statusPopover);
function toggle(k) {
  const next = open.value === k ? "" : k;
  // 换一盏灯就把上一次的动作反馈清掉：否则会出现"点开相机，却看到机器人上次的
  // 重连结果"这种串味提示（三块浮层共用同一行反馈位）。
  if (next !== open.value) { link.actionMsg = ""; link.claimError = ""; }
  ui.setStatusPopover(next);
}
function close() { ui.closeStatusPopover(); }

const pills = computed(() => link.pills);

/**
 * 动作反馈是"成功"还是"失败"。store 里只存一行纯文本（三个浮层共用这一行），
 * 这里按文案判定配色 —— 不把颜色塞进 store，是为了让 store 保持与 UI 无关。
 */
const actionErr = computed(() => /失败|不可用|未成功|未打开|权限/.test(link.actionMsg || ""));

async function doReconnect() {
  await link.reconnect();
}
async function doCamReconnect() {
  const ok = await cam.reconnect();
  // 成功只说结论；失败才说原因（相机服务没起 / 网线不在相机侧）。
  link.actionMsg = ok ? "连接成功" : (cam.error || "连接失败");
  await link.refresh();
}
async function doCamOpen() {
  await cam.open();
  await link.refresh();
  link.actionMsg = (link.camera && link.camera.opened)
    ? "相机已开启" : (cam.error || "连接失败：相机未打开");
}
async function claim(id) {
  const ok = await link.claimMode(id);
  if (ok) close();
}
</script>

<template>
  <div class="link-strip">
    <div v-for="p in pills" :key="p.key" class="link-wrap">
      <button class="link-pill" :class="[p.tone, { on: open === p.key }]"
              :title="p.title" @click="toggle(p.key)">
        <span class="lp-dot"></span>
        <Icon :name="p.icon" :size="13" />
        <span class="lp-label">{{ p.label }}</span>
        <span class="lp-value">{{ p.value }}</span>
        <Icon :name="open === p.key ? 'chevronUp' : 'chevronDown'" :size="11" class="lp-caret" />
      </button>

      <!-- ★★ 浮层必须挂在"被点的那一个灯"上：条件里同时判 open 与 p.key。
           只写 v-if="open === 'robot'" 是不够的 —— 这段在 v-for 里面，v-for 每一轮
           都会对这个条件求值一次，于是点任意一盏灯都会**三个灯各渲染一份同样的浮层**
           （症状：点一个开了三个，而且三层叠在一起）。 -->
      <div v-if="open === p.key" class="link-pop" :class="{ wide: p.key === 'run_mode' }">

        <!-- ---------- 机器人：地址 / 模式 / 重连 ---------- -->
        <template v-if="p.key === 'robot'">
          <div class="pop-head">
            <Icon name="plug" :size="14" />
            <span>机器人链路</span>
            <button class="pop-x" @click="close"><Icon name="close" :size="13" /></button>
          </div>
          <div class="pop-row"><span class="k">状态</span><span class="v">{{ p.value }}</span></div>
          <div class="pop-row" v-if="p.host">
            <span class="k">控制器</span><span class="v">{{ p.host }}:{{ p.port }}</span>
          </div>
          <div class="pop-row" v-if="p.simulate">
            <span class="k">simulate</span><span class="v">{{ p.simulate }}</span>
          </div>
          <p class="pop-tip">{{ p.detail }}</p>
          <div class="pop-btns">
            <button class="mini" :disabled="link.busy" @click="doReconnect">
              <Icon name="refresh" :size="12" /> 重新连接
            </button>
          </div>
          <p v-if="link.actionMsg" :class="actionErr ? 'pop-err' : 'pop-msg'">{{ link.actionMsg }}</p>
        </template>

        <!-- ---------- 摄像头：设备信息 / 重连 / 打开 ---------- -->
        <template v-else-if="p.key === 'camera'">
          <div class="pop-head">
            <Icon name="camera" :size="14" />
            <span>摄像头</span>
            <button class="pop-x" @click="close"><Icon name="close" :size="13" /></button>
          </div>
          <div class="pop-row"><span class="k">状态</span><span class="v">{{ p.value }}</span></div>
          <div class="pop-row" v-if="p.base"><span class="k">服务</span><span class="v">{{ p.base }}</span></div>
          <div class="pop-row" v-if="p.device"><span class="k">设备</span><span class="v">{{ p.device }}</span></div>
          <div class="pop-row" v-if="p.ip"><span class="k">相机 IP</span><span class="v">{{ p.ip }}</span></div>
          <div class="pop-row" v-if="p.opened">
            <span class="k">画面</span><span class="v">{{ p.resolution }} · {{ p.fps }} fps</span>
          </div>
          <p class="pop-tip">{{ p.detail }}</p>
          <p v-if="p.error" class="pop-err">{{ p.error }}</p>
          <div class="pop-btns">
            <button class="mini" :disabled="cam.busy" @click="doCamReconnect">
              <Icon name="refresh" :size="12" /> 重连相机
            </button>
            <button class="mini primary" v-if="!p.opened" :disabled="cam.busy" @click="doCamOpen">
              <Icon name="play" :size="12" /> 打开相机
            </button>
          </div>
          <p v-if="link.actionMsg" :class="actionErr ? 'pop-err' : 'pop-msg'">{{ link.actionMsg }}</p>
        </template>

        <!-- ---------- 示教器档位：来源说明 + 声明 ---------- -->
        <template v-else>
          <div class="pop-head">
            <Icon name="terminal" :size="14" />
            <span>示教器档位</span>
            <button class="pop-x" @click="close"><Icon name="close" :size="13" /></button>
          </div>

          <!-- ★ 来源必须写出来：现场看到"已确认"时要能分清是**硬件读到的**
               还是**人说的**，两者可信度完全不同。 -->
          <div class="pop-row">
            <span class="k">来源</span>
            <span class="v">{{ p.sourceText || "未确认" }}</span>
          </div>
          <div class="pop-row" v-if="p.source === 'controller'">
            <span class="k">实测于</span>
            <span class="v">{{ p.observedAgeSec === null ? "—" : p.observedAgeSec + " 秒前" }}</span>
          </div>
          <div class="pop-row" v-if="p.source === 'manual'">
            <span class="k">声明人</span>
            <span class="v">{{ p.actor || "—" }}</span>
          </div>

          <p class="pop-tip" v-if="p.source === 'controller'">
            档位由控制器状态字<b>实测</b>并自动确认，<b>不需要手动声明</b>。
            ★ 只有 AUTO / 远程 档下上位机点动与下发才有效；T1/T2 下控制器会忽略上位机指令。
          </p>
          <p class="pop-tip" v-else>
            控制器读不到档位（未上电 / 未连接 / 强制模拟），请按示教器上<b>旋钮</b>的实际位置声明。
            ★ 只有 AUTO / 远程 档下上位机点动与下发才有效。
          </p>

          <div class="pop-opts">
            <button v-for="o in p.options" :key="o.id" class="link-opt"
                    :class="{ on: p.confirmed && p.mode === o.id, dim: !link.hasToken }"
                    :disabled="link.busy || !link.hasToken" @click="claim(o.id)">
              <span class="lo-id">{{ o.id }}</span>
              <span class="lo-desc">{{ o.desc }}</span>
              <Icon v-if="p.confirmed && p.mode === o.id" name="check" :size="13" />
            </button>
          </div>
          <p class="pop-tip small" v-if="p.source === 'manual'">
            手动声明<b>不持久化</b>（后端重启后回到未确认），有效期 2 小时 —— 避免拿昨天的声明给今天授权。
            控制器连上后会自动实测，声明随即失效。
          </p>
          <div class="pop-btns">
            <button class="mini" v-if="p.source === 'manual'" :disabled="link.busy" @click="link.clearMode()">
              <Icon name="close" :size="12" /> 撤销声明
            </button>
            <button class="mini primary" v-if="!link.hasToken" @click="emit('request-control'); close()">
              <Icon name="lock" :size="12" /> 请求控制权限
            </button>
          </div>
          <p v-if="link.claimError" class="pop-err">{{ link.claimError }}</p>
          <p v-else-if="link.actionMsg" :class="actionErr ? 'pop-err' : 'pop-msg'">{{ link.actionMsg }}</p>
        </template>
      </div>
    </div>

    <div v-if="open" class="link-backdrop" @click="close"></div>
  </div>
</template>

<style scoped>
/* 状态灯固定在底栏左侧；右侧留给引导条与控制区（见 App.vue 的 #bottom-zone） */
.link-strip { display: flex; align-items: center; gap: 8px; flex: none; }
.link-wrap { position: relative; display: flex; }

.link-pill { display: inline-flex; align-items: center; gap: 6px; flex: none;
  padding: 6px 10px; border-radius: 8px; font-size: 12px; cursor: pointer;
  border: 1px solid var(--glass-line); background: var(--panel2);
  color: var(--muted); user-select: none; transition: border-color .14s ease, background .14s ease; }
.link-pill:hover { border-color: var(--accent); }
.link-pill.on { border-color: var(--accent); background: var(--accent-soft); }
.lp-dot { width: 8px; height: 8px; border-radius: 50%; flex: none; background: var(--muted); }
.lp-label { color: var(--muted); }
.lp-value { color: var(--txt); font-weight: 600; white-space: nowrap; }
.lp-caret { color: var(--muted); opacity: .8; }

/* ★ 每档色调都显式写出：绝不写 "缺配置就回退绿" 这种兜底 */
.link-pill.ok .lp-dot { background: var(--ok); box-shadow: 0 0 7px var(--ok); }
.link-pill.warn { color: var(--warn-fg); }
.link-pill.warn .lp-dot { background: var(--warn); box-shadow: 0 0 7px var(--warn); }
.link-pill.warn .lp-value { color: var(--warn-fg); }
.link-pill.err { color: var(--err-fg); border-color: var(--err-line);
  background: var(--err-soft); }
.link-pill.err .lp-dot { background: var(--err); box-shadow: 0 0 7px var(--err); }
.link-pill.err .lp-value { color: var(--err-fg); }
.link-pill.off .lp-dot { background: var(--muted); }
.link-pill.off .lp-value { color: var(--muted); }

/* 展开面板：向上浮出，不挤压 3D 视口高度 */
.link-pop { position: absolute; bottom: calc(100% + 8px); left: 0; z-index: 121;
  width: 320px; background: var(--panel); border: 1px solid var(--line);
  border-radius: 10px; box-shadow: var(--shadow-lg); padding: 10px; }
.link-pop.wide { width: 400px; }
.pop-head { display: flex; align-items: center; gap: 7px; font-size: 13px; font-weight: 700;
  color: var(--txt); }
.pop-head svg { color: var(--accent); }
.pop-x { flex: none; width: 22px; height: 22px; margin-left: auto; padding: 0;
  display: flex; align-items: center; justify-content: center;
  background: transparent; border: none; color: var(--muted); cursor: pointer; }
.pop-x:hover { color: var(--txt); }
.pop-row { display: flex; align-items: baseline; gap: 8px; font-size: 12px; padding: 3px 0; }
.pop-row .k { color: var(--muted); flex: none; width: 62px; }
.pop-row .v { color: var(--txt); font-variant-numeric: tabular-nums;
  word-break: break-all; min-width: 0; }
.pop-tip { font-size: 11px; color: var(--muted); line-height: 1.6; margin: 8px 0 0;
  padding-top: 8px; border-top: 1px solid var(--line); }
.pop-tip.small { border-top: none; padding-top: 2px; }
.pop-tip b { color: var(--txt); font-weight: 600; }
.pop-err { font-size: 11px; color: var(--err); margin: 8px 0 0; }
.pop-msg { font-size: 11px; color: var(--ok); margin: 8px 0 0; }
.pop-btns { display: flex; gap: 6px; margin-top: 10px; }
.pop-btns .mini { flex: 1; display: inline-flex; align-items: center; justify-content: center;
  gap: 5px; padding: 6px 8px; font-size: 12px; }
.mini.primary { background: var(--accent-btn); border-color: var(--accent); color: var(--accent); }
.mini:disabled { opacity: .45; cursor: not-allowed; }

/* 四档选择器 */
.pop-opts { display: flex; flex-direction: column; gap: 4px; margin: 8px 0 0; }
.link-opt { display: flex; align-items: center; gap: 8px; flex: none; width: 100%;
  text-align: left; padding: 6px 8px; font-size: 12px; border-radius: 6px;
  background: var(--panel2); border: 1px solid var(--line); color: var(--txt); }
.link-opt:hover { border-color: var(--accent); }
.link-opt.on { border-color: var(--accent); background: var(--accent-soft); }
.link-opt.on svg { color: var(--accent); margin-left: auto; }
.link-opt.dim { opacity: .5; cursor: not-allowed; }
.lo-id { flex: none; font-weight: 700; color: var(--accent); min-width: 52px; }
.lo-desc { flex: 1; min-width: 0; color: var(--muted); font-size: 11px; line-height: 1.4; }

/* 点击外部收起（放在面板下层，不吃面板上的点击） */
.link-backdrop { position: fixed; inset: 0; z-index: 120; }

/* ---- 窄屏：底栏元素变多，先牺牲文字标签，保住"灯 + 数值"这个信息核心 ---- */
@media (max-width: 1120px) {
  .lp-label { display: none; }
  .link-pill { padding: 5px 8px; gap: 5px; }
}
@media (max-width: 720px) {
  .link-pill { padding: 4px 7px; }
  .lp-caret { display: none; }
  /* 浮层宽度跟着视口收，别在窄屏上顶出右边界 */
  .link-pop, .link-pop.wide { width: min(340px, 92vw); left: 0; }
  .link-wrap:nth-last-child(n + 2) .link-pop { left: auto; right: 0; }
}
</style>
