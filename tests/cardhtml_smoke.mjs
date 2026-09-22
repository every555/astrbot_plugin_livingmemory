/** cardHtml 冒烟测试：三种状态各渲染一遍，未定义引用会当场炸 ReferenceError（8/19 橘子控制台抓到的洞） */
import { GatePage } from "../pages/dashboard/modules/gate-page.js";
import { esc } from "../pages/dashboard/modules/utils.js";

// 最小 stub：esc 用 document.createElement 做转义，cardHtml 不碰真 DOM
globalThis.document = { querySelector: () => null, createElement: () => ({ set textContent(v) { this._t = v; }, get innerHTML() { return this._t; } }) };

const page = new GatePage({}, { get: async () => ({}), post: async () => ({}) });

const cases = [
  { status: "candidate", verdict: "" },
  { status: "pending", verdict: "暂存", metadata: '{"actor":"webui","prev_verdict":"暂存"}' },
  { status: "confirmed", verdict: "升级", metadata: '{"actor":"reflection"}' },
  { status: "declined", verdict: "", metadata: '{"actor":"dialog","jaccard":0.1}' },
];

let ok = 0;
for (const c of cases) {
  const item = {
    id: 1, speaker: "春雪", content: "测试原句<>\"&", score: 0.9,
    axes: { fact: 0.9, emotion: 0.6, density: 0.5 },
    source: "chat", created_at: 1787111000.0, note: "批注", ...c,
  };
  try {
    const html = page.cardHtml(item, 0);
    if (typeof html !== "string" || !html.includes("gate-card")) throw new Error("输出异常");
    if (html.includes("undefined")) throw new Error("输出含 undefined 字面量");
    console.log(`[PASS] ${c.status} -> ${html.length} chars`);
    ok++;
  } catch (e) {
    console.error(`[FAIL] ${c.status}: ${e.message}`);
    process.exit(1);
  }
}
console.log(`${ok}/${cases.length} passed`);
