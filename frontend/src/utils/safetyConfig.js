// =====================================================================
// 安全围栏配置：出厂默认值 + 归一化（纯数据模块）。
//
// 不依赖 three / pinia / DOM，因此可以被 node 直接 import 做单元测试。
//
// ★ 为什么必须有 normalizeSafetyConfig：
//   配置来自后端 config/safety.json，而"地面碰撞检测"是后加的功能 ——
//   老配置文件里**根本没有 ground 字段**（本机就是这么个情况），
//   老前端也只往 ground 里写过 warn_mm，没有 colors/opacity。
//   这种"残缺配置"直接进渲染层，就会出现
//   「按状态取色取不到 → 全部回退成安全绿」→ 报警垫永远是绿的、
//   只有闪烁没有颜色。所以在进入渲染前，统一按默认值**深合并补齐**。
// =====================================================================

/** 与后端 default_safety() 保持一致；后端不可达时也用它兜底。 */
export function defaultSafety() {
  return {
    version: 1,
    enabled: true,
    zones: [
      {
        id: "z1", name: "主工作区", enabled: true, shape: "rect",
        center: { x: 0, z: 0 }, half: { x: 0.82, z: 0.82 }, radius: 0.9,
        corners: [[-0.82, -0.82], [0.82, -0.82], [0.82, 0.82], [-0.82, 0.82]],
        height: 1.2, walls: true,
        posts: { enabled: true, size: 0.1, color: "#2fd6f0" },
        thresholds: { basis: "halfwidth", warn: 0.3, danger: 0.1, fixed_mm: 300 },
        colors: { safe: "#2ecc71", warn: "#f2c500", danger: "#e5484d", hit: "#ff2020" },
        opacity: { safe: 0.11, warn: 0.2, danger: 0.3, hit: 0.45 },
      },
    ],
    blink: { hz: 4, min: 0.18, max: 0.63 },
    alarm: { banner: true, chip: true, banner_min: "danger", sound: false },
    walls: true,
    // 地面碰撞：J1 以上子树最低点**离立足面**（台面/地坪）的高度。报警垫铺满
    // "四个角柱围成的地面"（即区域多边形，矩形/正方形/四边形/圆形都跟随），
    // 因此不再有 radius。机器人站在站台上时，垫子会自动改铺到台面上。
    ground: {
      enabled: true, warn_mm: 150, danger_mm: 80, hit_mm: 30,
      colors: { safe: "#2ecc71", warn: "#f2c500", danger: "#e5484d", hit: "#ff2020" },
      opacity: { safe: 0.10, warn: 0.22, danger: 0.34, hit: 0.50 },
    },
    overlay: { bbox: false },
    camera: { position: [1.7, 1.35, 1.75], target: [0, 0.45, 0] },
  };
}

const isObj = (v) => !!v && typeof v === "object" && !Array.isArray(v);

/** 深合并：以 base 为骨架，用 over 覆盖（over 里缺的键保留 base 的值）。 */
function merge(base, over) {
  if (!isObj(over)) return Array.isArray(base) ? base.map(clone) : base;
  const out = isObj(base) ? Object.assign({}, base) : {};
  for (const k of Object.keys(over)) {
    const b = out[k], o = over[k];
    out[k] = isObj(b) && isObj(o) ? merge(b, o) : (Array.isArray(o) ? o.map(clone) : o);
  }
  return out;
}

function clone(v) {
  return v && typeof v === "object" ? JSON.parse(JSON.stringify(v)) : v;
}

/**
 * 把后端/旧版本/面板改残的配置补齐成"结构完整"的配置。
 * - 缺 ground / blink / alarm / walls / overlay / camera → 用默认值补
 * - 缺 ground.colors / ground.opacity → 用默认值补（修"报警垫永远绿色"）
 * - 每个 zone 缺 colors/opacity/posts/thresholds → 用默认区域补
 * @param {object} raw 后端返回或本地缓存的配置
 */
export function normalizeSafetyConfig(raw) {
  const d = defaultSafety();
  const c = merge(d, raw);
  const base = d.zones[0];
  const zones = Array.isArray(c.zones) ? c.zones : [];
  c.zones = zones.length
    ? zones.map((z, i) => {
        const zz = merge(base, z);
        zz.id = zz.id || "z" + (i + 1);
        zz.name = zz.name || "区域" + (i + 1);
        return zz;
      })
    : d.zones.map(clone);
  c.version = 1;
  return c;
}
