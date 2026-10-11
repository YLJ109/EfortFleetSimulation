<script setup>
// =====================================================================
// 底部状态区（状态片条）：运行日志 / 摄像头 / 示教器 / 真机链路。
//
// ★ 2026-10-11 用户口径（以这次为准）：
//   「底部的真机链路能不能跟机器人/摄像头/示教器那样样式…还有运行日志也放进去，
//     五个样式、状态，五选一显示，点别的地方自己隐藏，默认运行日志开启状态，
//     绿色/黄色/红色点要亮起来，运行日志放在最左侧。」
// ★★ 2026-10-11 第二轮用户口径（以这次为准）：
//   「底部有两个真机链路两个合并掉」—— 底部原来同时有「机器人 真实链路」片与
//   「真机链路 就绪」片**两个都是"真机链路"的入口**，合并为**一个**。
//   · 保留**内容更全**的那一份 = 「真机链路」片（`RcReadyCard`：双闸/档位/伺服/报警/
//     急停/程序/点动 + 一键就绪·取消就绪·清除报错）；
//   · 把原「机器人」片的**机器人链路内容**（真实链路/强制模拟/掉线降级 + 地址 +
//     simulate + 重新连接）**迁入「真机链路」片的面板**（不许丢功能）；
//   · 「真机链路」片的灯与文案**兼顾机器人链路**（链路坏了先报链路，链通才报就绪链）。
//
// 结构：
//   · 片（tab）从左到右： log(运行日志) · camera(摄像头) · run_mode(示教器) · link(真机链路)
//   · 多选一：点某片 → 只显示该片面板；**再点同一片收起**（toggle）
//   · 点面板/状态区以外的任何地方 → 自动收起（document 监听，**非阻塞**，不铺全屏遮罩）
//   · 默认开「运行日志」；**运行中（分拣/程序/AI 任一 running）自动开日志且不允许整体隐藏**
//
// 复用：
//   · 摄像头/示教器两片沿用 linkview.js → link.pills 的既有样式与判据（一字未改）；
//   · 真机链路片的面板内容 = **原样复用 RcReadyCard**（就绪链）+ 机器人链路块。
//
// 灯的判据（全部取自真实字段，取不到就是灰，绝不兜底成绿）见 docs 方案文档 B.3。
// 状态 → 外观的映射：摄像头/示教器走 src/linkview.js（纯函数，node 可断言）；
//   日志片走最新一条日志级别；真机链路片走 stores/rcReady.js 的寄存器快照（+ 机器人链路灯）。
// 数据来自 stores/link.js（唯一 /system/guide 轮询者）等既有 store，不新增轮询者。
// =====================================================================
import { ref, computed, onMounted, onBeforeUnmount, watch, nextTick } from "vue";
import Icon from "./Icon.vue";
import RcReadyCard from "./RcReadyCard.vue";
import { useLinkStore } from "../stores/link.js";
// ★ 2026-10-11：底部「机器人」片并入「真机链路」片后，真机链路片的灯要**兼顾机器人链路** ——
//   复用 linkview.js 的 toneOf（与摄像头/示教器同一套"未知绝不回退成绿"的取色口径）。
import { toneOf } from "../linkview.js";
import { useCameraStore } from "../stores/camera.js";
import { useUiStore } from "../stores/ui.js";
import { useExecStore } from "../stores/exec.js";
import { useRcReadyStore } from "../stores/rcReady.js";
import { useSorting3dStore } from "../stores/sorting3d.js";

const emit = defineEmits(["request-control"]);

const link = useLinkStore();
const cam = useCameraStore();
const ui = useUiStore();
const exec = useExecStore();
const rc = useRcReadyStore();
const s3d = useSorting3dStore();

/**
 * 当前展开的片（"" = 全部收起）。五片共用一个展开位，避免并排弹出挡住视口。
 * ★ 状态放在 ui store 而不是局部 ref：全局键盘处理要知道"现在有没有**用户主动打开的**
 *   浮层"，才能在按 Esc 时先关浮层而不是直接急停（见 ui.popoverModal）。
 */
const open = computed(() => ui.statusPopover);

/** 有执行在跑（程序/点位/序列 -> exec.running；分拣/AI/文件 -> sorting3d.running）。 */
const anyRunning = computed(() => exec.running || s3d.running);

// ---------------------------------------------------------------------
// 运行日志片：灯 = **最新一条日志的级别**（真实字段 exec.log[last].level）。
//   ok/info → 绿；warn → 黄；err → 红；无日志 → 灰。
// ---------------------------------------------------------------------
const logTab = computed(() => {
  const n = exec.log.length;
  const last = n ? exec.log[n - 1] : null;
  const tone = !last ? "off"
    : last.level === "err" ? "err"
      : last.level === "warn" ? "warn" : "ok";
  return {
    key: "log", icon: "terminal", label: "运行日志",
    value: n ? (n + " 条") : "空",
    tone,
    title: last ? ("运行日志（最新）：" + last.text) : "运行日志：暂无",
  };
});

// ---------------------------------------------------------------------
// 真机链路片：灯 = **机器人与就绪链的综合状态**。
//   ★ 2026-10-11「两个真机链路合并」后，本片要**同时**代表**机器人链路**（原来的
//     「机器人」片）与**控制器就绪链**（RcReadyCard）—— 取两者里**最差**的那一档：
//     · 机器人链路（来自 stores/link.js 的 /system/guide）：真实链路=绿；
//       强制模拟=黄；掉线降级/未连接=红；拿不到=灰。
//     · 就绪链（来自 stores/rcReady.js 的寄存器快照，原来的判据）：
//       红 = 读不到快照 / 报警 / 急停 / 非 AUTO·远程档；
//       绿 = 完整就绪（伺服+程序+运行+AUTO/远程+无报警+非急停）；
//       黄 = 其余（有快照、无故障，但伺服未上电或程序未加载 = 中间态）。
//   value：**链路不通先报链路**（链断了谈不上就绪），链路正常才报就绪链结论。
// ---------------------------------------------------------------------
const TONE_RANK = { off: 0, ok: 1, warn: 2, err: 3 };
function worseTone(a, b) { return TONE_RANK[a] >= TONE_RANK[b] ? a : b; }

/** 机器人链路灯（原「机器人」片的取色口径，一字未改）。 */
function robotTone() {
  const r = link.robot;
  if (!r || !r.state) return "off";         // 拿不到链路状态 → 灰（不假装正常）
  return toneOf(r.level);                    // ok/warn/err/off 由 linkview 统一映射
}
function rcTone() {
  if (!rc.ok) return "err";                 // ★ 读不到控制器寄存器快照（模拟/离线/未连接）
  const b = rc.bits || {};
  if (b.alarm || b.estop) return "err";     // ★ 报警 / 急停
  if (!rc.joggable) return "err";           // ★ 非 AUTO/远程档：上位机指令无效
  if (rc.ready) return "ok";                // ★ 完整就绪
  return "warn";                            // 中间态（正在就绪 / 条件未满足）
}
const linkTab = computed(() => {
  const rt = robotTone();                    // 机器人链路档
  const ct = rcTone();                       // 控制器就绪链档
  const t = worseTone(rt, ct);               // 取最差（链路坏了照样能报出来，不丢"机器人灯"）
  const robot = link.robot || null;
  const rlab = (robot && robot.label) || "未知";
  const robotBad = (rt === "err" || rt === "warn");   // 机器人链路不正常 → 先报链路
  const readyValue = !rc.ok ? "读不到"
    : rc.ready ? "就绪"
      : (rc.servoOn ? "未就绪" : "未上电");
  const value = robotBad ? rlab : readyValue;
  const bits = ["机器人 " + rlab, "就绪链 " + readyValue];
  return {
    key: "link", icon: "plug", label: "真机链路",
    value,
    tone: t,
    // ★ B4：片 value 是"一句话结论"，展开面板里是「机器人链路 + 逐项就绪链」—— 在 title 里点明关系。
    title: "真机链路（含机器人链路）—— " + bits.join(" · ")
      + "\n展开面板：机器人链路（地址/重连）+ 详细就绪链"
      + (rc.error ? "\n" + rc.error : ""),
  };
});

/** 状态片：运行日志（最左）→ 摄像头/示教器（link.pills，剔除已并入真机链路的「机器人」片）
 *  → 真机链路（最右）。 */
const tabs = computed(() => [
  logTab.value,
  ...link.pills.filter((p) => p.key !== "robot"),   // ★ 机器人片已并入真机链路片（2026-10-11）
  linkTab.value,
]);

// ---------------------------------------------------------------------
// 多选一：切换 / 收起。
// ---------------------------------------------------------------------
function toggle(k) {
  const next = open.value === k ? "" : k;
  // 换一片就把上一片的动作反馈清掉：否则会出现"点开相机，却看到机器人上次的
  // 重连结果"这种串味提示（几块面板共用同一行反馈位）。
  if (next !== open.value) { link.actionMsg = ""; link.claimError = ""; }
  // ★ 运行中不允许把「运行日志」整体收起来（用户："运行中一定可见"）。
  if (next === "" && anyRunning.value && k === "log") { ui.setStatusPopover("log"); return; }
  ui.setStatusPopover(next);
}
function close() {
  // 运行中即便点"外部"也不让全藏（保证运行日志可见）。
  if (anyRunning.value) { ui.setStatusPopover("log"); return; }
  ui.closeStatusPopover();
}

// ---- 点面板/状态区以外的任何地方 → 自动收起（非阻塞：页面照常可点） ----
function onDocMouseDown(ev) {
  if (!open.value) return;
  const t = ev.target;
  // 用 #bottom-zone 判定"状态区以内"：点控制区(播报/请求控制)也算区内，不收面板。
  if (t && t.closest && t.closest("#bottom-zone")) return;
  close();
}
onMounted(() => document.addEventListener("mousedown", onDocMouseDown, true));
onBeforeUnmount(() => document.removeEventListener("mousedown", onDocMouseDown, true));

// ---- 运行中自动开日志（rising edge 打开；运行中不许整体隐藏） ----
watch(anyRunning, (r) => { if (r && open.value !== "log") ui.setStatusPopover("log"); });
watch(() => ui.statusPopover, (v) => {
  if (!v && anyRunning.value) ui.setStatusPopover("log");   // 运行中不让"全部隐藏"
});

// ---------------------------------------------------------------------
// 运行日志：自动跟随最新（"粘底"，用户往上翻看历史时不抢滚动条）。
// ---------------------------------------------------------------------
const logBody = ref(null);
const logStick = ref(true);
const LOG_BOTTOM_EPS = 24;
function onLogScroll() {
  const el = logBody.value;
  if (!el) return;
  logStick.value = (el.scrollHeight - el.scrollTop - el.clientHeight) <= LOG_BOTTOM_EPS;
}
async function logScrollToBottom() {
  await nextTick();
  const el = logBody.value;
  if (el) el.scrollTop = el.scrollHeight;
}
watch(() => exec.log.length, () => { if (logStick.value) logScrollToBottom(); });
watch(open, (k) => { if (k === "log") { logStick.value = true; logScrollToBottom(); } });

/** 各片面板宽度：日志要宽一点看得清，真机链路要放得下 RcReadyCard，其余沿用默认。 */
function popStyle(k) {
  if (k === "log") return { width: "min(460px, 92vw)" };
  if (k === "link") return { width: "min(420px, 92vw)" };
  return null;
}

/**
 * 动作反馈是"成功"还是"失败"。store 里只存一行纯文本（几块面板共用这一行），
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
  // ★ 用户手动开相机 = 要看画面 → 顺带打开「实时捕捉」（真实监控页的取流开关，
  //   默认关）；与相机面板的「开启相机」同一口径。）
  cam.setLiveCapture(true);
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
    <div v-for="p in tabs" :key="p.key" class="link-wrap">
      <button class="link-pill" :class="[p.tone, { on: open === p.key }]"
              :title="p.title" @click="toggle(p.key)">
        <span class="lp-dot"></span>
        <Icon :name="p.icon" :size="13" />
        <span class="lp-label">{{ p.label }}</span>
        <span class="lp-value">{{ p.value }}</span>
        <Icon :name="open === p.key ? 'chevronUp' : 'chevronDown'" :size="11" class="lp-caret" />
      </button>

      <!-- ★★ 浮层必须挂在"被点的那一个片"上：条件里同时判 open 与 p.key。
           只写 v-if="open === 'robot'" 是不够的 —— 这段在 v-for 里面，v-for 每一轮
           都会对这个条件求值一次，于是点任意一片都会**五片各渲染一份同样的浮层**
           （症状：点一个开了五个，而且五层叠在一起）。 -->
      <div v-if="open === p.key" class="link-pop" :class="{ wide: p.key === 'run_mode' }"
           :style="popStyle(p.key)">

        <!-- ---------- 运行日志：最新在底、自动粘底、可清空 ---------- -->
        <template v-if="p.key === 'log'">
          <div class="pop-head">
            <Icon name="terminal" :size="14" />
            <span>运行日志 ({{ exec.log.length }})</span>
            <button class="pop-x" @click="close"><Icon name="close" :size="13" /></button>
          </div>
          <div class="lp-log-body" ref="logBody" @scroll="onLogScroll">
            <div v-if="!exec.log.length" class="lp-log-empty">暂无日志</div>
            <div v-for="(l, i) in exec.log" :key="i" class="lp-log-line" :class="l.level">
              <span class="lp-log-time">{{ l.t }}</span>
              <span class="lp-log-text">{{ l.text }}</span>
            </div>
          </div>
          <div class="pop-btns">
            <button class="mini" @click="exec.clearLog()">
              <Icon name="trash" :size="12" /> 清空日志
            </button>
          </div>
        </template>

        <!-- ---------- 真机链路：机器人链路（原「机器人」片）+ 完整 RcReadyCard ---------- -->
        <template v-else-if="p.key === 'link'">
          <button class="pop-x lp-rc-close" @click="close" aria-label="收起">
            <Icon name="close" :size="13" />
          </button>
          <!-- ★ 2026-10-11「两个真机链路合并」：原「机器人」片的面板内容**整体迁到这里**，
               不再单独占一个片（不丢功能：真实链路/强制模拟/掉线降级 + 控制器地址 + 重连）。 -->
          <div class="lp-robot" :class="robotTone()">
            <div class="lp-robot-head">
              <Icon name="plug" :size="13" />
              <span>机器人链路</span>
              <span class="lp-robot-val">{{ (link.robot && link.robot.label) || "未知" }}</span>
            </div>
            <div class="pop-row" v-if="link.robot && link.robot.host">
              <span class="k">控制器</span>
              <span class="v">{{ link.robot.host }}:{{ link.robot.port }}</span>
            </div>
            <div class="pop-row" v-if="link.robot && link.robot.simulate">
              <span class="k">simulate</span><span class="v">{{ link.robot.simulate }}</span>
            </div>
            <p class="pop-tip" v-if="link.robot && link.robot.detail">{{ link.robot.detail }}</p>
            <div class="pop-btns">
              <button class="mini" :disabled="link.busy" @click="doReconnect">
                <Icon name="refresh" :size="12" /> 重新连接
              </button>
            </div>
            <p v-if="link.actionMsg" :class="actionErr ? 'pop-err' : 'pop-msg'">{{ link.actionMsg }}</p>
          </div>
          <hr class="lp-sep" />
          <RcReadyCard />
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
  </div>
</template>

<style scoped>
/* 状态片固定在底栏左侧；右侧留给控制区（见 App.vue 的 #bottom-zone） */
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

/* ---- 运行日志面板：整体沿用底栏日志的观感（等宽、按级别配色、粘底） ---- */
.lp-log-body { max-height: 180px; overflow-y: auto; margin-top: 6px;
  font-size: 11px; line-height: 1.6; font-family: Consolas, monospace; }
.lp-log-empty { color: var(--muted); padding: 6px 2px; }
.lp-log-line { display: flex; gap: 8px; padding: 2px 0; white-space: nowrap; overflow: hidden; }
.lp-log-time { flex: none; color: var(--muted); font-variant-numeric: tabular-nums; }
.lp-log-text { color: var(--txt); min-width: 0; text-overflow: ellipsis; overflow: hidden; }
.lp-log-line.ok .lp-log-text { color: var(--ok); }
.lp-log-line.warn .lp-log-text { color: var(--warn); }
.lp-log-line.err .lp-log-text { color: var(--err); }
.lp-log-line.info .lp-log-text { color: var(--accent); }

/* ---- 真机链路面板：RcReadyCard 自带卡片外观，这里只把收起按钮浮到右上 ---- */
.lp-rc-close { position: absolute; top: 8px; right: 8px; z-index: 2; margin-left: 0; }
.link-pop .card :deep(h3) { padding-right: 26px; }

/* ★ 2026-10-11：真机链路片里新增的「机器人链路」块（原「机器人」片内容迁入）。
   顶部一条分隔线把它与下面的 RcReadyCard（就绪链）分开；灯色跟着 robotTone 走。 */
.lp-robot { padding-right: 26px; }
.lp-robot-head { display: flex; align-items: center; gap: 6px; font-size: 12px; font-weight: 700;
  color: var(--txt); padding-bottom: 6px; border-bottom: 1px solid var(--line); }
.lp-robot-head svg { color: var(--accent); }
.lp-robot-val { margin-left: auto; font-weight: 600; }
.lp-robot.ok .lp-robot-val { color: var(--ok); }
.lp-robot.warn .lp-robot-val { color: var(--warn); }
.lp-robot.err .lp-robot-val { color: var(--err); }
.lp-robot.off .lp-robot-val { color: var(--muted); }
.lp-sep { border: none; border-top: 1px solid var(--line); margin: 8px 0; }

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
