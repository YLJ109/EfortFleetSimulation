// =====================================================================
// 机房卡片文案与等级判定（纯函数，不依赖 Vue / three / DOM）。
//
// 为什么抽出来：大屏上 12 张卡片要显示"这台是什么状态"，而两个牌子的状态位
// **字段名根本不同**（埃夫特 alarm/estop/running/servo_on，华沿 error_code/
// moving/enabled/inpos）。判据写在 .vue 里就会变成 12 份 v-if 各说各话，
// 而且 node 断言不到 —— 状态色是安全相关的显示，必须能被测试钉住。
//
// 字段口径来源（不要凭记忆改）：
//   backend/app/services/drivers.py:184  埃夫特 rc_snapshot → status
//   backend/app/services/drivers.py:398  华沿 DataSheet     → status
// =====================================================================

/** 卡片配色等级，从"最不打扰"到"最该看"。 */
export const LEVEL_RANK = {
  offline: -1, safe: 0, warn: 1, danger: 2, hit: 3, alarm: 4,
};

/**
 * 三个来源合成一个等级：在线与否 + 控制器状态位 + 围栏/地面评估。
 * @param {boolean} online     本帧是否在线
 * @param {object}  status     驱动上报的状态位（两个牌子字段不同，见上）
 * @param {string}  safety     three 层算出的 safe/warn/danger/hit（没算过则空）
 *
 * ★ 控制器报警排在围栏碰撞之上：围栏红是"快碰到了"（还能退），
 *   报警/急停是"这台已经出事了"，大屏要先让人看见它。
 */
export function levelOf(online, status, safety) {
  if (!online) return "offline";
  const st = status || {};
  if (st.alarm || st.estop || (Number(st.error_code) || 0) !== 0) return "alarm";
  return safety === "hit" || safety === "danger" || safety === "warn" ? safety : "safe";
}

/** 是否"正在动"：两个牌子各有一个字段，都认。 */
export function movingOf(status) {
  const st = status || {};
  return !!(st.running || st.moving);
}

/** 埃夫特状态位 → 一句人话。 */
function efortText(st) {
  if (st.read_error) return "状态位读取失败";
  if (st.estop) return "急停";
  if (st.alarm) return "报警";
  if (st.running) {
    const p = st.program;
    return p === undefined || p === null ? "运行中" : `运行中 · 程序 ${p}`;
  }
  if (!st.servo_on) return "伺服未上电";
  const mode = [];
  if (st.auto) mode.push("自动");
  if (st.remote) mode.push("远程");
  return mode.length ? `就绪 · ${mode.join("/")}` : "就绪 · 手动";
}

/** 华沿状态位 → 一句人话。 */
function elfinText(st) {
  const code = Number(st.error_code) || 0;
  if (code !== 0) {
    const axis = Number(st.error_axis) || 0;
    return axis ? `报警 E${code} · J${axis}` : `报警 E${code}`;
  }
  if (st.moving) return "运动中";
  if (st.enabled && st.inpos) return "就绪 · 已到位";
  if (st.enabled) return "已使能";
  if (st.via) return `就绪 · ${st.via}`;   // 兜底通道（ReadActCoord）只有坐标，没有状态位
  return "未使能";
}

/** 状态文案。brand 认不出来时按"两个牌子都试一遍"给保守结果。 */
export function statusText(brand, status) {
  const st = status || {};
  const b = String(brand || "").toUpperCase();
  if (b === "EFORT") return efortText(st);
  if (b === "HUAYAN") return elfinText(st);
  return Object.keys(st).length ? "状态未知" : "还没读到状态位";
}

/** 速度档位：埃夫特是百分比，华沿是 override（同为百分比口径）。 */
export function speedOf(status) {
  const st = status || {};
  const v = st.speed_pct !== undefined && st.speed_pct !== null
    ? Number(st.speed_pct) : Number(st.override);
  return Number.isFinite(v) ? Math.round(v) : null;
}

/**
 * 示教器档位：有没有打到 AUTO（自动/远程）。
 * 两个牌子字段不同：埃夫特是 auto（另有 remote 表示远程许可），华沿是 auto_mode。
 * @returns {boolean|null} null = 这个牌子/这一帧**根本没报**这个字段
 *   ★ 必须区分"手动"和"没读到"：把没数据显示成"手动"，现场会以为示教器打错档了
 *     去拧旋钮，而真正的问题可能是这台压根没连上。
 */
export function autoOf(status) {
  const st = status || {};
  const v = st.auto !== undefined ? st.auto : st.auto_mode;
  if (v === undefined || v === null || v === "") return null;
  return !(v === false || v === 0 || v === "0");
}

const round = (v, n) => {
  const x = Number(v);
  if (!Number.isFinite(x)) return null;
  const k = 10 ** n;
  return Math.round(x * k) / k;
};

/**
 * "3 秒前" 这种人话。
 * ★ 帧停了但 online 还是 true 的情况（后端卡死 / 半开链路）必须看得见，
 *   否则大屏上 12 台都显示"在线·就绪"，实际画面早冻住了。
 */
export function ageText(ms) {
  if (!Number.isFinite(ms) || ms < 0) return "—";
  if (ms < 1000) return "刚刚";
  if (ms < 60000) return `${Math.floor(ms / 1000)} 秒前`;
  return `${Math.floor(ms / 60000)} 分钟前`;
}

/**
 * 顶部报警条（大屏）。**不复用** safetyLabels 的话术：单机那套说的是"距围栏余量"，
 * 大屏要先回答的是"**哪一台**出事了" —— 12 台里只报一句"危险"等于没报。
 * @returns {{state:string,text:string,sub:string}|null} null = 全场无需报警
 */
export function fleetBanner(cards, summary) {
  if (!Array.isArray(cards) || !cards.length) return null;
  let worst = null, n = 0;
  for (const c of cards) {
    const r = LEVEL_RANK[c.level] ?? -1;
    if (r < LEVEL_RANK.warn) continue;
    n++;
    if (!worst || r > (LEVEL_RANK[worst.level] ?? -1)) worst = c;
  }
  if (!worst) return null;
  // CSS 只有 safe/warn/danger/hit 四档配色：控制器报警借用"碰撞"那档（最刺眼的红）
  const state = worst.level === "alarm" ? "hit" : worst.level;
  const who = worst.short || worst.name;
  const head = {
    alarm: `机房报警：${who} ${worst.statusText}`,
    hit: `碰撞报警：${who} 已触及安全围栏`,
    danger: `危险：${who} 即将碰撞安全围栏`,
    warn: `注意：${who} 接近安全围栏`,
  }[worst.level];
  const text = n > 1 ? `${head}（等 ${n} 台）` : head;
  const s = summary || {};
  const total = Number(s.total) || cards.length;
  const online = Number.isFinite(Number(s.online)) ? Number(s.online)
    : cards.filter((c) => c.online).length;
  const offline = total - online;
  const parts = [`在线 ${online}/${total}`];
  if (Number(s.alarm)) parts.push(`报警 ${s.alarm} 台`);
  if (Number(s.moving)) parts.push(`运动 ${s.moving} 台`);
  if (offline > 0) parts.push(`离线 ${offline} 台`);
  return { state, text, sub: parts.join(" · ") };
}

/**
 * 读数格式化：null / NaN 一律给破折号，**绝不给 0** ——
 * 大屏上 12 台并排，一个假的 0.00 会被当成真读数（"这台在零位"）。
 * @param {*} v 值
 * @param {number} d 小数位
 */
export function fmtNum(v, d = 2) {
  if (v === null || v === undefined) return "—";
  const x = Number(v);
  return Number.isFinite(x) ? x.toFixed(d) : "—";
}

/**
 * 短代号（大屏卡片抬头用）。登记名是"A区-下组-1号(最前)"这种长串，12 张卡并排时
 * 会被省略号切成"A区-下组-…"，读不出是哪一台。
 * ★ 优先用 store 算好的**区内连续编号**（A1…A6 / B1…B6，用户口径）；
 *   没有它时（比如单独测 makeCard）才退回"区+排+槽位"，保证永远有个能读的代号。
 */
export function shortName(e) {
  const g = e.group === "lower" ? "下" : e.group === "upper" ? "上" : "";
  const slot = e.slot === undefined || e.slot === null ? "" : e.slot;
  return `${e.zone || "?"}${g}${slot}`;
}

/**
 * 链路态的一行话：在线 / 仿真 / 离线。
 * ★ "仿真"必须和"在线"长得不一样、和"离线"也不一样：
 *   操作员在沙盘里拖了一台没连真机的机器人，卡片若还写"离线"，他会以为画面在骗人；
 *   若写成"在线"，更糟 —— 那是在谎报真机在位。
 */
export function linkLabel(online, sim) {
  if (sim) return "仿真";
  return online ? "在线" : "离线";
}

/**
 * 有没有报错、报的是什么码。
 * 两个牌子的故障口径不同：埃夫特只有布尔位（alarm / estop / read_error），
 * 华沿给的是数字码 + 轴号（error_code / error_axis）。
 * @returns {null|{code:string, text:string}} null = 没有故障（卡片显示"正常"）
 *   ★ 返回 null 和返回"读取失败"必须分开：读不到状态位是**链路问题**，
 *     把它显示成"正常"会让操作员以为这台没事就走了。
 */
export function faultOf(brand, status) {
  const st = status || {};
  const b = String(brand || "").toUpperCase();
  if (b === "HUAYAN") {
    const code = Number(st.error_code) || 0;
    if (!code) return null;
    const axis = Number(st.error_axis) || 0;
    return { code: "E" + code + (axis ? " · J" + axis : ""), text: "控制器报警" };
  }
  if (b === "EFORT") {
    if (st.estop) return { code: "E-STOP", text: "急停按下" };
    if (st.alarm) return { code: "ALARM", text: "控制器报警" };
    if (st.read_error) return { code: "LINK", text: "状态位读取失败" };
    return null;
  }
  // 认不出的牌子：有 error_code 就按华沿口径报，没有就不下结论
  const code = Number(st.error_code) || 0;
  return code ? { code: "E" + code, text: "控制器报警" } : null;
}

/**
 * 组装一张卡片的数据（**纯数据**，模板只负责排版）。
 * @param {object} entry   /api/fleet 的 robots[i]（名单/坐标/IP，机房地图口径）
 * @param {Array<number>|null} joints 六轴角度（度）—— 取自帧缓冲，与 3D 同源
 * @param {object|null} snap 帧缓冲里这台的最近状态快照
 * @param {string} safety three 层给出的围栏等级
 * @param {object} opt  { now, modelLabel, unverified }
 */
export function makeCard(entry, joints, snap, safety, opt = {}) {
  const now = Number(opt.now) || Date.now();
  const st = (snap && snap.status) || {};
  const online = !!(snap && snap.online);
  const at = snap && Number.isFinite(snap.at) ? snap.at : 0;
  const ageMs = at ? now - at : Infinity;
  const tcp = snap && snap.tcp;
  const cart = snap && snap.cart;
  // ★ 一个状态位都没读到时，**不能**套牌子的话术：efortText({}) 会返回
  //   "伺服未上电"，那是编的（我们根本没读到伺服位）。大屏上这行字是唯一的安全
  //   出口，宁可直说"没读到"，也不要给一个听起来像真读数的结论。
  const txt = Object.keys(st).length
    ? statusText(entry.brand, st)
    : (online ? "在线 · 还没读到状态位" : "离线 · 无数据");
  return {
    id: entry.id,
    name: entry.name,
    short: opt.short || shortName(entry),
    zone: entry.zone,
    group: entry.group,
    slot: entry.slot,
    order: entry.order,
    brand: entry.brand,
    model: entry.model,
    modelLabel: opt.modelLabel || entry.model,
    unverified: !!opt.unverified,
    host: entry.host,
    port: entry.port,
    hostPort: `${entry.host}:${entry.port}`,
    online,
    // 本地沙盘驱动 与 后端合成帧 都算"仿真"：两者的共同点是**不是真机读数**
    sim: !!opt.simDriven || !!(snap && snap.simulated),
    linkLabel: linkLabel(online, !!opt.simDriven),
    stale: !!(snap && snap.stale),
    simulated: !!(snap && snap.simulated),
    frozen: online && Number.isFinite(ageMs) && ageMs > 3000,
    level: levelOf(online, st, safety),
    safety: safety || "safe",
    statusText: txt,
    fault: faultOf(entry.brand, st),
    moving: movingOf(st),
    speed: speedOf(st),
    auto: autoOf(st),
    q: Array.isArray(joints) ? joints.slice(0, 6).map((v) => round(v, 2)) : [null, null, null, null, null, null],
    tcp: tcp ? [round(tcp[0], 1), round(tcp[1], 1), round(tcp[2], 1)] : null,
    cart: cart ? cart.slice(0, 6).map((v, i) => round(v, i < 3 ? 1 : 2)) : null,
    error: (snap && snap.error) || "",
    ageMs: Number.isFinite(ageMs) ? Math.round(ageMs) : null,
    ageText: ageText(ageMs),
  };
}
