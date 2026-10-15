// =====================================================================
// 机房设置页的纯函数层：草稿比对 + 前置校验。
//
// ★ 为什么单独一个文件而不是写在 .vue 里：这两件事都必须**能被 node 断言直接跑**
//   （见 tools/verify_fleet_registry.mjs）。写在 SFC 里就只能靠人肉点界面，
//   而"保存时到底把哪几台发出去了"这类逻辑错了，症状是**改错了机器**，
//   在机房里比界面崩掉严重得多。
//
// ★ 这里只是"少发无意义的改动 + 提前给一句人话"，**不是**权限或安全边界：
//   真正的判据在后端（core/fleet_settings.check_updates + FleetConfig 整表校验），
//   前端放过什么，后端照样会拒。两边规则刻意保持一致，但以后端为准。
// =====================================================================

/** 一台上可改的字段（与后端 ROBOT_FIELDS 同源）。id 不在里面：id 是主键。 */
export const EDITABLE = ["name", "host", "port", "model", "zone", "group", "slot", "order"];

const NAME_MAX_DEFAULT = 40;

/** IPv4 点分四段，每段 0~255。 */
export function ipOk(v) {
  const m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(String(v == null ? "" : v).trim());
  return !!m && m.slice(1).every((g) => Number(g) >= 0 && Number(g) <= 255);
}

/** 数值化：空串/undefined 视为"没填"（null），非法数字视为 NaN 交给校验拦下。 */
function numOf(v) {
  if (v === null || v === undefined || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : NaN;
}

/**
 * 草稿 vs 生效值 → 只把**真正改过**的字段发出去。
 *
 * ★ 为什么要裁：后端覆盖层是稀疏的（只存与 fleet.yaml 不同的项）。
 *   前端若把 12 行原值整包发上去，稀疏化会把它们全部当"相等"丢掉，
 *   看着没事，实际后果是"改过的字段如果恰好等于默认值，界面显示成没改过" ——
 *   而这正是操作员要确认的事。逐字段比对能让"待保存 N 处改动"这个数字是真的。
 *
 * @param {Array} rows   生效登记表（/api/fleet/config 的 robots[]）
 * @param {Object} draft id → 该行草稿
 * @returns {Array<{id:string, [field:string]: any}>}
 */
export function diffRows(rows, draft) {
  const out = [];
  for (const r of rows || []) {
    const d = (draft || {})[r.id];
    if (!d) continue;
    const patch = { id: r.id };
    let touched = false;
    for (const k of EDITABLE) {
      if (!(k in d)) continue;
      const now = norm(k, r[k]);
      const next = norm(k, d[k]);
      if (next === null && now === null) continue;      // 空对空 = 没改
      if (String(next) !== String(now)) { patch[k] = d[k]; touched = true; }
    }
    if (touched) out.push(patch);
  }
  return out;
}

function norm(k, v) {
  if (v === null || v === undefined) return null;
  if (k === "name" || k === "host") return String(v).trim();
  if (k === "zone") return String(v).toUpperCase();
  if (k === "group") return String(v).toLowerCase();
  if (k === "port" || k === "slot" || k === "order") { const n = numOf(v); return Number.isNaN(n) ? v : n; }
  return String(v);
}

/** 阵列改动：同样只发不同的那几键。 */
export function diffLayout(base, draft) {
  const out = {};
  for (const [k, v] of Object.entries(draft || {})) {
    if (v === null || v === undefined || v === "") continue;
    const n = Number(v);
    if (!Number.isFinite(n)) { out[k] = v; continue; }          // 交给校验报错
    if (Number(base && base[k]) !== n) out[k] = n;
  }
  return out;
}

/**
 * 前置校验：返回 [{ id, field, msg }]，空数组 = 可以发。
 * 一次报全，不修一个弹一个 —— 现场是在一张表里改十几行。
 */
export function validateRows(rows, draft, meta = {}) {
  const errs = [];
  const models = (meta.models || []).map((m) => m.key);
  const size = Number(meta.group_size) || 0;
  const nameMax = Number(meta.name_max) || NAME_MAX_DEFAULT;
  const byId = {};
  for (const r of rows || []) byId[r.id] = r;

  // 编号唯一是**跨行**判据：先把"草稿生效后的全场编号"摊出来。
  // 只盯着被改的那几行的话，"把 A1-1 改成 5"撞上本来就叫 5 的 A2-2 会被漏掉
  // —— 后端一定会拒（整表校验），但那已经是点完保存之后了。
  const orderOwner = {};
  for (const r of rows || []) {
    const d = (draft || {})[r.id] || {};
    const n = "order" in d ? numOf(d.order) : numOf(r.order);
    if (n !== null && !Number.isNaN(n) && n >= 1) orderOwner[n] = r.id;
  }

  for (const [id, d] of Object.entries(draft || {})) {
    const base = byId[id];
    if (!base) { errs.push({ id, field: "id", msg: "登记表里没有这台" }); continue; }
    if ("name" in d) {
      const s = String(d.name ?? "").trim();
      if (!s) errs.push({ id, field: "name", msg: "名称不能为空" });
      else if (s.length > nameMax) errs.push({ id, field: "name", msg: `名称最长 ${nameMax} 字` });
    }
    if ("host" in d && !ipOk(d.host)) errs.push({ id, field: "host", msg: "IP 要点分四段（如 192.168.1.12）" });
    if ("port" in d) {
      const n = numOf(d.port);
      if (n === null || Number.isNaN(n) || n < 1 || n > 65535) errs.push({ id, field: "port", msg: "端口要 1~65535" });
    }
    if ("model" in d && models.length && !models.includes(String(d.model))) {
      errs.push({ id, field: "model", msg: `型号 ${d.model} 不在档案里` });
    }
    if ("zone" in d && !((meta.zones || ["A", "B"]).includes(String(d.zone).toUpperCase()))) {
      errs.push({ id, field: "zone", msg: "zone 只能是 A 或 B" });
    }
    if ("group" in d && !((meta.groups || ["lower", "upper"]).includes(String(d.group).toLowerCase()))) {
      errs.push({ id, field: "group", msg: "group 只能是 lower 或 upper" });
    }
    if ("slot" in d) {
      const n = numOf(d.slot);
      if (n === null || Number.isNaN(n) || n < 1 || (size && n > size)) {
        errs.push({ id, field: "slot", msg: `slot 要 1~${size || "?"}（每排台数由 yaml 定）` });
      }
    }
    if ("order" in d) {
      const n = numOf(d.order);
      const owner = n !== null && !Number.isNaN(n) ? orderOwner[n] : null;
      if (n === null || Number.isNaN(n) || n < 1) errs.push({ id, field: "order", msg: "编号必须是正整数" });
      else if (owner && owner !== id) errs.push({ id, field: "order", msg: `编号 ${n} 已被 ${owner} 占着` });
    }
  }
  return errs;
}

/** 某台是否被改过（列表右侧那颗"改过"的小标记）。 */
export function rowTouched(row, draft) {
  const d = (draft || {})[row.id];
  return !!d && diffRows([row], draft).length > 0;
}
