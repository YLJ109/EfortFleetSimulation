<script setup>
// 大屏上的一台机器人 = 一张卡。
//
// 取色/形态口径（与用户给的参考图一致，全站只用 index.html 里的设计令牌）：
//   · 左侧一条**等级色条**：一眼扫过去先看颜色，再看字（12 张卡并排时字太小）
//   · 切角（clip-path）而不是圆角，右上角斜切；选中时四角出 L 形角标
//   · 数值用窄体技术字 --num，数字大、单位小且下挂
//   · 半透明底浮在 3D 场景之上（不把场景挤走）；卡片**不做** backdrop-filter，见样式里那条性能注释
//
// ★ 只读：整张卡唯一的交互是"点一下让相机推过去"，没有任何下发入口。
import { fmtNum } from "../utils/fleetCards.js";

const props = defineProps({
  c: { type: Object, required: true },        // utils/fleetCards.makeCard 的产物
  sel: { type: Boolean, default: false },
  levelLabel: { type: String, default: "" },
});
const emit = defineEmits(["pick"]);

const AXIS = ["J1", "J2", "J3", "J4", "J5", "J6"];

function onKey(e) {
  if (e.key === "Enter" || e.key === " ") { e.preventDefault(); emit("pick", props.c); }
}
</script>

<template>
  <div class="fc" :class="[c.level, { sel, off: !c.online }]"
       role="button" tabindex="0"
       :aria-label="`${c.name} ${c.statusText}`"
       :title="`${c.name} · ${c.modelLabel} · ${c.hostPort}`"
       @click="emit('pick', c)" @keydown="onKey">
    <i class="fc-bar"></i>
    <b class="cn tl"></b><b class="cn tr"></b><b class="cn bl"></b><b class="cn br"></b>

    <!-- 抬头：短代号（A下1）+ IP。登记名太长，12 张卡并排时只会剩一串省略号。 -->
    <div class="fc-h">
      <span class="fc-name">{{ c.short }}</span>
      <span class="fc-full">{{ c.name }}</span>
      <em class="lk" :class="c.sim ? 'sim' : c.online ? 'on' : 'off'"
          :title="c.sim ? '这台没连真机，画面是本地沙盘摆位，不是真机姿态'
                        : c.online ? '真机链路在流' : '没有链路数据'">{{ c.linkLabel }}</em>
      <span class="fc-ip">{{ c.hostPort }}</span>
    </div>
    <div class="fc-sub">
      {{ c.modelLabel }}
      <em v-if="c.unverified" class="bd unv">机型待核</em>
      <em v-if="c.simulated" class="bd sim">模拟</em>
      <em v-if="c.frozen" class="bd fro">帧已冻结</em>
      <em v-else-if="c.stale" class="bd fro">数据陈旧</em>
    </div>

    <!-- 状态短语：等级色点 + 一句人话（两个牌子字段不同，文案在 fleetCards 里合成）。
         右侧是示教器档位：AUTO / 手动 / —（没报这个字段，不能瞎写成"手动"）。 -->
    <div class="fc-st">
      <i class="fc-dot"></i>
      <span class="fc-stx">{{ c.statusText }}</span>
      <span class="fc-rgt">
        <em class="flt" :class="c.fault ? 'bad' : 'good'"
            :title="c.fault ? (c.fault.text + '（' + c.fault.code + '）') : '控制器没有上报任何故障位'">
          <template v-if="c.fault">{{ c.fault.code }} {{ c.fault.text }}</template>
          <template v-else>正常</template>
        </em>
        <em class="md" :class="c.auto === true ? 'on' : c.auto === false ? 'off' : 'na'"
            :title="c.auto === true ? '示教器在 AUTO（自动/远程）：上位机可以下发'
                    : c.auto === false ? '示教器在手动档：上位机下发会被控制器拒绝'
                    : '这一帧没有上报档位（离线或未实现该字段），不当作手动'">
          {{ c.auto === true ? "AUTO" : c.auto === false ? "手动" : "档位 —" }}
        </em>
        <em v-if="c.moving" class="mv">运动</em>
      </span>
    </div>

    <!-- 六轴读数：与 3D 同源（都读 net/fleetBuffer），不会出现"画面在动、数字不动" -->
    <div class="fc-q">
      <div v-for="(a, i) in AXIS" :key="a" class="qa">
        <i>{{ a }}</i>
        <b>{{ fmtNum(c.q[i], 2) }}</b>
      </div>
    </div>

    <!-- 底行：速度 + 更新时间 + 等级；TCP 单独一行（三个数加单位在 236px 列里放不下，
         硬挤只会把数字切成省略号，那比换行更糟）。 -->
    <div class="fc-f">
      <span class="g">
        <i>速度</i>
        <b v-if="c.speed !== null" class="sp">{{ c.speed }}<u>%</u></b>
        <b v-else class="dim">—</b>
      </span>
      <span class="g age" :class="{ old: c.frozen }">
        <i>更新</i><b>{{ c.ageText }}</b>
      </span>
      <span class="lv">{{ levelLabel }}</span>
    </div>
    <div class="fc-f">
      <span class="g tcp">
        <i>TCP</i>
        <b v-if="c.tcp">{{ fmtNum(c.tcp[0], 1) }} / {{ fmtNum(c.tcp[1], 1) }} / {{ fmtNum(c.tcp[2], 1) }}<u>mm</u></b>
        <b v-else class="dim">—</b>
      </span>
    </div>
  </div>
</template>

<style scoped>
.fc { position: relative; cursor: pointer; user-select: none;
  padding: 7px 9px 6px 11px;
  /* ★ 卡片**不用** backdrop-filter：12 张卡 + 2 块面板各自做高斯模糊，
     等于每帧让合成器重算 14 片模糊区，鼠标一动就掉帧（这页"卡卡的"主要在这）。
     面板层（状态环 / 详情 / 控制台）数量少、面积大，才保留模糊。 */
  background: var(--glass-solid);
  border: 1px solid var(--glass-line);
  box-shadow: 0 0 0 1px transparent;
  /* 切角：右上斜切 10px（这套风格的辨识度所在，不用 border-radius） */
  clip-path: polygon(0 0, calc(100% - 10px) 0, 100% 10px, 100% 100%, 0 100%);
  transition: background .14s ease, border-color .14s ease, transform .14s ease; }
.fc:hover { background: var(--panel2); border-color: var(--accent-line);
  transform: translateX(1px); }
.fc:focus-visible { outline: none; border-color: var(--accent-line); }
/* 左右两列的卡片朝向中间，靠 translateX 表达"被推向视口" */
.right .fc:hover { transform: translateX(-1px); }

/* ---- 等级色条 ---- */
.fc-bar { position: absolute; left: 0; top: 0; bottom: 0; width: 3px;
  background: var(--muted); }
.fc.safe .fc-bar { background: var(--ok); }
.fc.warn .fc-bar { background: var(--warn); }
.fc.danger .fc-bar { background: var(--err); }
.fc.hit .fc-bar, .fc.alarm .fc-bar { background: var(--hit);
  box-shadow: 0 0 10px var(--hit); }
.fc.hit, .fc.alarm { border-color: var(--err-line); background: var(--err-soft); }
/* 报警/碰撞要"刺眼"：整卡呼吸闪，静态区不加戏，只在出事的那张动 */
.fc.hit .fc-bar, .fc.alarm .fc-bar { animation: fc-blink 1s steps(2, end) infinite; }
@keyframes fc-blink { 50% { opacity: .25; } }

/* ---- 选中：四角 L 形角标（参考图的 HUD 描边） ---- */
.cn { position: absolute; width: 7px; height: 7px; opacity: .35;
  border: 0 solid var(--accent); transition: opacity .14s ease; pointer-events: none; }
.cn.tl { left: 1px; top: 1px; border-left-width: 2px; border-top-width: 2px; }
.cn.tr { right: 1px; top: 1px; border-right-width: 2px; border-top-width: 2px; }
.cn.bl { left: 1px; bottom: 1px; border-left-width: 2px; border-bottom-width: 2px; }
.cn.br { right: 1px; bottom: 1px; border-right-width: 2px; border-bottom-width: 2px; }
.fc.sel { background: var(--panel2); border-color: var(--accent-line); }
.fc.sel .cn { opacity: 1; }
.fc.sel.hit .cn, .fc.sel.alarm .cn { border-color: var(--hit); }

/* ---- 抬头 ---- */
.fc-h { display: flex; align-items: baseline; gap: 6px; }
.fc-name { flex: none; font-family: var(--num); font-size: 15px; font-weight: 700;
  letter-spacing: 1.2px; color: var(--txt); }
.fc-full { font-size: 9px; color: var(--muted); min-width: 0;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.fc.safe .fc-name { color: var(--ok); }
.fc.warn .fc-name { color: var(--warn-fg); }
.fc.danger .fc-name, .fc.hit .fc-name, .fc.alarm .fc-name { color: var(--err-fg); }
.fc-ip { margin-left: auto; flex: none; font-size: 10px; font-family: var(--num);
  color: var(--muted); letter-spacing: .2px; }
/* 链路态：在线绿 / 离线灰 / 仿真橙 —— 仿真必须和在线区分开，也不能和离线混同 */
.lk { font-style: normal; flex: none; font-size: 9px; padding: 0 4px; border-radius: 3px;
  letter-spacing: .5px; border: 1px solid; }
.lk.on { color: var(--ok); border-color: var(--ok-line); background: var(--ok-soft); }
.lk.off { color: var(--muted); border-color: var(--line); background: var(--cap); }
.lk.sim { color: var(--warn); border-color: var(--warn-line); background: var(--warn-soft); }
.fc-sub { display: flex; align-items: center; gap: 5px; margin-top: 1px;
  font-size: 9px; letter-spacing: 1.4px; text-transform: uppercase;
  color: var(--muted); white-space: nowrap; overflow: hidden; }
.bd { font-style: normal; letter-spacing: .2px; text-transform: none;
  font-size: 9px; padding: 0 4px; border-radius: 3px; border: 1px solid; }
.bd.unv { color: var(--c-config); border-color: var(--c-config-line); background: var(--c-config-soft); }
.bd.sim { color: var(--warn-fg); border-color: var(--warn-line); background: var(--warn-soft); }
.bd.fro { color: var(--err-fg); border-color: var(--err-line); background: var(--err-soft); }

/* ---- 状态行 ---- */
.fc-st { display: flex; align-items: center; gap: 5px; margin-top: 5px;
  padding-top: 4px; border-top: 1px solid var(--glass-line); font-size: 11px; }
.fc-dot { flex: none; width: 6px; height: 6px; border-radius: 50%; background: var(--muted); }
.fc.safe .fc-dot { background: var(--ok); box-shadow: 0 0 6px var(--ok); }
.fc.warn .fc-dot { background: var(--warn); box-shadow: 0 0 6px var(--warn); }
.fc.danger .fc-dot { background: var(--err); box-shadow: 0 0 6px var(--err); }
.fc.hit .fc-dot, .fc.alarm .fc-dot { background: var(--hit); box-shadow: 0 0 8px var(--hit); }
.fc-stx { color: var(--txt2); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.fc.danger .fc-stx, .fc.hit .fc-stx, .fc.alarm .fc-stx { color: var(--err-fg); font-weight: 700; }
.fc-rgt { margin-left: auto; flex: none; display: flex; align-items: baseline; gap: 5px; }
.mv { font-style: normal; font-size: 9px; padding: 0 4px; border-radius: 3px;
  color: var(--accent); border: 1px solid var(--accent-line); background: var(--accent-soft); }
/* 示教器档位徽标：AUTO 才代表"上位机说话管用"，所以给绿；手动给灰蓝；没数据给虚线边框 */
.md { font-style: normal; font-size: 9px; padding: 0 4px; border-radius: 3px;
  letter-spacing: .4px; border: 1px solid; }
.md.on { color: var(--ok); border-color: var(--ok-line); background: var(--ok-soft); }
.md.off { color: var(--txt2); border-color: var(--line); background: var(--cap); }
.md.na { color: var(--muted); border-color: var(--glass-line); border-style: dashed; }
/* 故障位：正常给绿、报警给红并**带上错误码**（现场报修只说"红了"没用，要码） */
.flt { font-style: normal; flex: none; font-size: 9px; padding: 0 4px; border-radius: 3px;
  letter-spacing: .4px; border: 1px solid; font-family: var(--num); }
.flt.good { color: var(--ok); border-color: var(--ok-line); background: var(--ok-soft); }
.flt.bad { color: var(--ink); border-color: var(--err); background: var(--err);
  font-weight: 700; animation: fc-blink 1.1s steps(2, end) infinite; }
.sp { color: var(--accent); font-weight: 700; }

/* ---- 六轴读数 ---- */
.fc-q { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 1px 8px; margin-top: 5px; }
.qa { display: flex; align-items: baseline; gap: 4px; min-width: 0; }
.qa i { font-style: normal; font-size: 9px; color: var(--muted); flex: none; }
.qa b { font-family: var(--num); font-size: 12px; font-weight: 600;
  color: var(--txt); font-variant-numeric: tabular-nums;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.fc.off .qa b { color: var(--muted); }

/* ---- 底行 ---- */
.fc-f { display: flex; align-items: baseline; gap: 8px; margin-top: 5px;
  padding-top: 4px; border-top: 1px solid var(--glass-line);
  font-size: 10px; min-width: 0; }
/* 第二行（TCP）不再画分隔线：两行是一组，中间插一条线会读成两个区块 */
.fc-f + .fc-f { border-top: none; padding-top: 0; margin-top: 2px; }
.fc-f .g { display: flex; align-items: baseline; gap: 3px; min-width: 0; }
.fc-f .g i { font-style: normal; font-size: 9px; letter-spacing: .8px;
  color: var(--muted); flex: none; }
.fc-f .g b { font-family: var(--num); font-size: 11px; color: var(--accent);
  font-variant-numeric: tabular-nums; white-space: nowrap;
  overflow: hidden; text-overflow: ellipsis; }
.fc-f .g b u { text-decoration: none; font-size: 8px; margin-left: 2px; color: var(--muted); }
.fc-f .g b.dim { color: var(--muted); }
.fc-f .age { margin-left: auto; flex: none; }
.fc-f .age b { color: var(--muted); font-size: 10px; }
.fc-f .age.old b { color: var(--err-fg); }
.lv { flex: none; font-size: 9px; letter-spacing: .6px; color: var(--muted); }
.fc.warn .lv { color: var(--warn-fg); }
.fc.danger .lv, .fc.hit .lv, .fc.alarm .lv { color: var(--err-fg); }

/* 窄列（侧栏被拖窄 / 小屏）时六轴改两列，避免数字被省略号吃掉 */
@container (max-width: 210px) {
  .fc-q { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
</style>
