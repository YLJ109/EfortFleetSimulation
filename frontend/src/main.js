import { createApp } from "vue";
import { createPinia } from "pinia";
import App from "./App.vue";
import { BRAND } from "./brand.js";
import { useAuthStore } from "./stores/auth.js";

// 标签页标题：从 brand.js 取，避免和静态 <title>（首屏 hydration 前显示）各写一份。
// 静态那份必须同步改，回归里有断言钉着（verify_link.mjs 的 L 段）。
document.title = `${BRAND.name}(${BRAND.room})`;

const pinia = createPinia();
const app = createApp(App).use(pinia);

// ★ 挂载前先校验一次控制令牌。
//   sessionStorage 里的令牌可能是上一次会话留下的，后端一重启它就作废了；
//   而子组件的 onMounted 早于 App 的 onMounted，若不先验，执行页/审计页会在
//   挂载瞬间拿着死令牌打出一批 401（浏览器控制台必报，JS 侧屏蔽不掉）。
//   先对齐一次，坏令牌当场清掉，之后所有控制请求都会被 apiControl 拦在本地。
//   ★ 最多等 1.5s：后端没起来时 fetchStatus 会静默兜底，但也不能让首屏一直白着。
const auth = useAuthStore(pinia);
const verified = auth.fetchStatus().catch(() => {});
const budget = new Promise((resolve) => setTimeout(resolve, 1500));
Promise.race([verified, budget]).then(() => app.mount("#app"));

// ★ 审计修复 P1-D5：全站此前没有任何全局错误处理。
//   未捕获异常 / 未处理的 Promise 拒绝只在控制台留一行红字 —— 现场的人不会开
//   开发者工具，只会看到"页面点了没反应"。这里统一兜底：
//     ① console.error 原样打出（保留堆栈，排障还是要靠它）；
//     ② 落一份内存环形缓冲（最近 50 条），控制台里随时可取：
//          window.__efortErrors
//   刻意不做 toast 弹窗：轮询类失败可能每几秒重复一次，弹窗会把界面刷没；
//   真正影响操作的错误（下发失败、权限失效）都由各自的 store 走日志行/结果区。
const _uncaught = [];
function noteUncaught(kind, err) {
  try {
    _uncaught.push({
      kind,
      msg: (err && err.stack) || (err && err.message) || String(err),
      at: new Date().toISOString(),
    });
    if (_uncaught.length > 50) _uncaught.shift();
    console.error("[EFORT 未捕获" + (kind === "rejection" ? "的 Promise 拒绝" : "异常") + "]", err);
  } catch (e) { /* 兜底处理自身绝不能抛 */ }
}
if (typeof window !== "undefined") {
  window.addEventListener("error", (ev) => noteUncaught("error", ev && (ev.error || ev.message)));
  window.addEventListener("unhandledrejection", (ev) => noteUncaught("rejection", ev && ev.reason));
  // 排障入口：控制台执行 window.__efortErrors() 看最近 50 条
  Object.defineProperty(window, "__efortErrors", {
    value: () => _uncaught.slice(),
    writable: false,
  });
}