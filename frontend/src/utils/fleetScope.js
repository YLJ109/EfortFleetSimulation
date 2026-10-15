// =====================================================================
// 机房大屏的"作用域"（一页看哪几台）—— 纯函数，可被 node 断言。
//
// ★ 为什么只有一份 scope 而不是六套布局：
//   参考图那种大屏最容易做成"每个页面一套模板"，结果是列方向、编号方向、
//   统计口径在三处各写一遍，改一处漏两处。这里把 scope 定义成
//   "一组 id + 左右两列怎么分"，其余全部由它推导：
//     · 卡片列     → scopeColumns()
//     · KPI 统计   → scopeIds() 喂给现有卡片数组过滤
//     · 3D 相机   → 由 ids 的量包围盒算等轴机位
//     · 控制作用域 → 只能 ⊆ 当前 scope（看不见就不许动）
//
// 两条不可变的方向约定（换任何 scope 都不变，否则操作员每换一个页面就要重学一次）：
//   1) 左列 = A 区 / 上排，右列 = B 区 / 下排；
//   2) 列内编号恒为**倒序**（A6→A1），与等轴画面"上排在屏幕上方"对齐。
// =====================================================================

export const SCOPES = [
  { key: "all", label: "全部", en: "ALL 12", hint: "12 台同框" },
  { key: "upper", label: "上排", en: "ROW UPPER", hint: "两区的上排 6 台" },
  { key: "lower", label: "下排", en: "ROW LOWER", hint: "两区的下排 6 台" },
  { key: "zoneA", label: "A 区", en: "ZONE A", hint: "埃夫特 6 台" },
  { key: "zoneB", label: "B 区", en: "ZONE B", hint: "华沿 6 台" },
];

export const SCOPE_BY_KEY = Object.fromEntries(SCOPES.map((s) => [s.key, s]));

/** 这台在不在当前作用域里。*/
export function inScope(card, scope) {
  switch (scope) {
    case "upper": return card.group === "upper";
    case "lower": return card.group === "lower";
    case "zoneA": return card.zone === "A";
    case "zoneB": return card.zone === "B";
    case "all": return true;
    // 认不出的作用域（手改 localStorage）退回"全部"，而不是"一台都不显示" ——
    // 空屏会被当成后端挂了。
    default: return true;
  }
}

/** 当前作用域里的工位 id 列表（相机取景与控制作用域都用它）。 */
export function scopeIds(cards, scope) {
  return (cards || []).filter((c) => inScope(c, scope)).map((c) => c.id);
}

/**
 * 左右两列怎么分。每列是一串"组块"（同区/同排的卡片 + 它的标题），
 * 模板直接两层 v-for 就能画，不需要在 .vue 里再判一次方向。
 * @returns {{left: object[], right: object[]}}
 */
export function scopeColumns(cards, scope) {
  const rows = (cards || []).filter((c) => inScope(c, scope));
  // 单看一个区时按"上排 / 下排"分左右；跨区时按"A 区 / B 区"分左右。
  const byGroup = scope === "zoneA" || scope === "zoneB";
  const desc = (a, b) => (Number(b.order) || 0) - (Number(a.order) || 0);
  const out = { left: [], right: [] };
  const sides = byGroup ? [["left", "upper"], ["right", "lower"]]
                        : [["left", "A"], ["right", "B"]];
  for (const [side, anchor] of sides) {
    const pool = rows.filter((c) => (byGroup ? c.group === anchor : c.zone === anchor));
    // 跨区看时列内还要再分"上排 / 下排"两块（列里得有小标题，不然 6 张卡糊成一串）；
    // 单区看时整列就是那一排，一块到底。
    for (const g of (byGroup ? [anchor] : ["upper", "lower"])) {
      const list = pool.filter((c) => c.group === g);
      if (!list.length) continue;
      const zone = byGroup ? list[0].zone : anchor;
      out[side].push({
        key: `${side}-${zone}-${g}`, side, zone, group: g,
        cards: list.slice().sort(desc),
      });
    }
  }
  // 预期外的 zone/group（有人改了 fleet.yaml）也要露出来，不能被静默吞掉
  const known = new Set([...out.left, ...out.right].flatMap((b) => b.cards.map((c) => c.id)));
  const rest = rows.filter((c) => !known.has(c.id));
  if (rest.length) {
    out.right.push({ key: "right-other", side: "right", zone: "?", group: "?",
      cards: rest.slice().sort(desc) });
  }
  return out;
}

/**
 * 作用域对应的控制意图（发给后端 /fleet/scope）。
 * ★ 只报 kind + 锚点，ids 由服务端按名单现算 —— 前端这份 cards 可能已经过期。
 */
export function scopeRequest(scope) {
  switch (scope) {
    case "upper": return { kind: "row", group: "upper" };
    case "lower": return { kind: "row", group: "lower" };
    case "zoneA": return { kind: "zone", zone: "A" };
    case "zoneB": return { kind: "zone", zone: "B" };
    default: return { kind: "all" };
  }
}

export function scopeLabel(scope) {
  const s = SCOPE_BY_KEY[scope];
  return s ? s.label : "全部";
}

/** 选中的台还在作用域里吗？不在就该取消选中（否则控制台浮层对着一个画面里看不见的台）。 */
export function scopeKeeps(selCard, scope) {
  return !!selCard && inScope(selCard, scope);
}
