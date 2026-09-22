/**
 * Gate Page - 安检门（话分量感知系统）
 * 标注台：候选卡片流 + 裁决（通过/驳回/暂存）+ 键盘流水线（J/K 1/2/3）
 * 战报：水位数字 + 分数分布直方图 + 毕业词 + 试秤盒
 *
 * 设计原则（橘子 2026-08-19）：实用至上——原句全文不截断、真实数字、不做花架子。
 * 裁决与省察调度器走同一条落库路，网页只是把老婆的手延伸到这里。
 */

import { esc } from "./utils.js";

const STATUS_TEXT = {
  candidate: "待审",
  confirmed: "已通过",
  declined: "已驳回",
  pending: "暂存",
  merged: "已合并",
  absorbed: "已豁免",
};

export class GatePage {
  constructor(state, apiClient) {
    this.state = state;
    this.api = apiClient;

    // 标注台状态
    this.filter = { status: "candidate", speaker: "" };
    this.sort = "score"; // score|time|fact|emotion|density|id（8/19 橘子要的排序）
    this.order = "desc"; // desc=高→低 | asc=低→高
    this.items = [];
    this.page = 1;
    this.total = 0;
    this.hasMore = false;
    this.loading = false;
    this.subTab = "review"; // review | stats
    this.focusIndex = -1;

    // 战报缓存
    this.statsCache = null;

    // 分数段过滤（橘子 2026-08-20：分布图点击分桶跳标注台）
    this.filter.scoreMin = null;  // null=未启用；数字=左闭
    this.filter.scoreMax = null;  // null=无上限（最后一桶 1.0+）
    this._statsTimer = null;      // 战报 30s 轮询
    this._chipBound = false;      // chip 清除按钮事件委托只挂一次
  }

  /** 切页入口 */
  async fetch() {
    // chip 清除按钮：document 级事件委托，挂一次（renderList 重建 DOM 也不丢）
    if (!this._chipBound) {
      this._chipBound = true;
      document.addEventListener("click", (e) => {
        if (e.target.closest && e.target.closest("#gate-chip-clear")) this.clearScoreFilter();
      });
    }
    // 战报 30s 轮询（橘子 2026-08-20：不要打开才看一眼死数；页面切走不空刷）
    if (this._statsTimer) clearInterval(this._statsTimer);
    this._statsTimer = setInterval(() => {
      if (this.subTab !== "stats") return;
      const el = document.getElementById("gate-stats-body");
      if (!el || el.offsetParent === null) return; // gate 页被切走(display:none)
      this.loadStats();
    }, 30000);
    await Promise.all([this.loadCandidates(true), this.loadStats()]);
  }

  // ==================== 数据加载 ====================

  async loadCandidates(reset = false) {
    if (this.loading) return;
    this.loading = true;
    if (reset) {
      this.page = 1;
      this.items = [];
      this.renderListShell();
    }
    try {
      const params = {
        page: String(this.page),
        page_size: "30",
        sort: this.sort,
        order: this.order,
      };
      if (this.filter.status && this.filter.status !== "all") {
        params.status = this.filter.status;
      }
      if (this.filter.speaker) params.speaker = this.filter.speaker;
      if (this.filter.scoreMin != null) params.score_min = String(this.filter.scoreMin);
      if (this.filter.scoreMax != null) params.score_max = String(this.filter.scoreMax);

      const data = await this.api.get("gate/candidates", params);
      const newItems = Array.isArray(data.items) ? data.items : [];
      this.items = reset ? newItems : this.items.concat(newItems);
      this.total = data.total || 0;
      this.hasMore = !!data.has_more;
      this.page += 1;
    } catch (e) {
      console.error("[gate] 候选加载失败:", e);
    } finally {
      this.loading = false;
      this.render();
    }
  }

  async loadStats() {
    try {
      const data = await this.api.get("gate/stats");
      this.statsCache = data;
      this.renderStatsPanel();
    } catch (e) {
      console.error("[gate] 战报加载失败:", e);
    }
    this.loadAutonomous();
  }

  /** 自主存档台账（#1791）：老婆豁免登记过的原句，橘子的翻案权入口 */
  async loadAutonomous() {
    const el = document.getElementById("gate-auto-list");
    if (!el) return;
    try {
      const data = await this.api.get("gate/autonomous", { limit: "30" });
      const items = Array.isArray(data.items) ? data.items : [];
      if (!items.length) {
        el.innerHTML = `<span class="gate-muted">还没有自主存档——老婆每次亲手 memorize 后会把原句登记在这里</span>`;
        return;
      }
      el.innerHTML = items
        .map(
          (it) => `
        <div class="gate-auto-item" data-fpid="${it.id}">
          <div class="gate-auto-head">
            <span class="gate-cid">豁免#${it.id}</span>
            <span class="gate-muted">${it.created_at ? new Date(it.created_at * 1000).toLocaleString("zh-CN", { hour12: false }) : "--"}</span>
            <button class="btn btn-sm gate-auto-revoke">撤销豁免</button>
          </div>
          <div class="gate-auto-body">${esc(it.content || "")}</div>
        </div>`
        )
        .join("");
      el.querySelectorAll(".gate-auto-revoke").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const item = btn.closest(".gate-auto-item");
          const fpid = Number(item.dataset.fpid);
          if (!confirm("撤销这条豁免？撤销后同类内容会被门重新拦下纳管（不删记忆本体）")) return;
          btn.disabled = true;
          try {
            const r = await this.api.post("gate/autonomous/revoke", { fp_id: fpid });
            if (r && r.status !== "error") item.remove();
            else {
              btn.disabled = false;
              alert((r && r.message) || "撤销失败");
            }
          } catch (e) {
            btn.disabled = false;
            alert("撤销失败: " + e.message);
          }
        });
      });
    } catch (e) {
      el.innerHTML = `<span class="gate-muted">台账加载失败: ${esc(e.message || String(e))}</span>`;
    }
  }

  // ==================== 裁决 ====================

  async doVerdict(cid, action) {
    const card = document.querySelector(`.gate-card[data-id="${cid}"]`);
    const noteInput = card ? card.querySelector(".gate-note-input") : null;
    const wordSel = card ? card.querySelector(".gate-word-sel") : null;
    const body = {
      candidate_id: Number(cid),
      action,
      note: noteInput ? noteInput.value.trim() : "",
      word: wordSel ? wordSel.value : "",
    };
    const btns = card ? card.querySelectorAll("button") : [];
    btns.forEach((b) => (b.disabled = true));
    try {
      const result = await this.api.post("gate/verdict", body);
      if (result && result.status !== "error" && card) {
        card.classList.add("gate-card-done");
        const label = STATUS_TEXT[action === "confirm" ? "confirmed" : action === "decline" ? "declined" : action] || "已裁决";
        const mid = result.memory_id ? ` · 已入库 #${result.memory_id}` : "";
        card.querySelector(".gate-card-actions").innerHTML =
          `<span class="gate-done-text">${label}${mid}</span>`;
      } else {
        btns.forEach((b) => (b.disabled = false));
        alert((result && result.message) || "裁决失败");
      }
    } catch (e) {
      btns.forEach((b) => (b.disabled = false));
      alert("裁决失败: " + e.message);
    }
  }

  /** 捞回暂存：pending → candidate 重新排队（8/19 橘子暂存 #291 后消失的洞） */
  async doRestore(cid) {
    const card = document.querySelector(`.gate-card[data-id="${cid}"]`);
    const btns = card ? card.querySelectorAll("button") : [];
    btns.forEach((b) => (b.disabled = true));
    try {
      const result = await this.api.post("gate/restore", { candidate_id: Number(cid) });
      if (result && result.status !== "error") {
        if (card) card.remove();
        this.total = Math.max(0, this.total - 1);
        if (this.filter.status === "pending" && !document.querySelector(".gate-card")) {
          this.loadCandidates(true);
        }
      } else {
        btns.forEach((b) => (b.disabled = false));
        alert((result && result.message) || "捞回失败");
      }
    } catch (e) {
      btns.forEach((b) => (b.disabled = false));
      alert("捞回失败: " + e.message);
    }
  }

  // ==================== 试秤 ====================

  async doScore() {
    const input = document.getElementById("gate-score-input");
    const out = document.getElementById("gate-score-result");
    if (!input || !out) return;
    const text = input.value.trim();
    if (!text) {
      out.innerHTML = `<span class="gate-muted">先输入一句话再按「过秤」</span>`;
      return;
    }
    out.innerHTML = `<span class="gate-muted">过秤中……</span>`;
    try {
      const data = await this.api.post("gate/score", { text });
      const axes = data.axes || {};
      const bar = (name, label) => this.axisBar(label, axes[name] || 0);
      out.innerHTML = `
        <div class="gate-score-out">
          <div class="gate-score-total">总分 <b>${(data.score ?? 0).toFixed(2)}</b></div>
          ${bar("fact", "事实")}${bar("emotion", "情感")}${bar("density", "密度")}
          <div class="gate-muted">阈值 0.45 起拦 · ${(data.score ?? 0) >= 0.45 ? "<b style='color:var(--warning,#e67700)'>这句话会被拦下进候选区</b>" : "这句话会飘走（分量不够）"}</div>
        </div>`;
    } catch (e) {
      out.innerHTML = `<span class="gate-muted">过秤失败: ${esc(e.message || String(e))}</span>`;
    }
  }

  // ==================== 渲染 ====================

  render() {
    const root = document.getElementById("page-gate");
    if (!root) return;
    const shell = root.querySelector(".gate-shell");
    if (!shell) return;
    this.renderList();
    this.renderStatsPanel();
  }

  renderListShell() {
    const listEl = document.getElementById("gate-list");
    if (listEl) listEl.innerHTML = `<div class="gate-muted gate-center">加载中……</div>`;
  }

  renderList() {
    const listEl = document.getElementById("gate-list");
    if (!listEl) return;
    const chip = this.scoreChipHtml();

    if (!this.items.length) {
      listEl.innerHTML = chip + `
        <div class="gate-empty">
          <div class="gate-empty-num">${this.total}</div>
          <div>${this.filter.status === "candidate" ? "门很安静——没有待审候选，粮仓空着呢" : "这个筛选下没有记录"}</div>
        </div>`;
      this.renderMoreBtn();
      return;
    }

    listEl.innerHTML = chip + this.items.map((it, idx) => this.cardHtml(it, idx)).join("");
    this.renderMoreBtn();
    this.bindCardEvents();
  }

  cardHtml(it, idx) {
    const axes = it.axes || {};
    const st = STATUS_TEXT[it.status] || it.status || "";
    const speakerCls = it.speaker === "春雪" ? "gate-sp-ai" : "gate-sp-user";
    const score = Number(it.score || 0);
    const time = it.created_at
      ? new Date(it.created_at * 1000).toLocaleString("zh-CN", { hour12: false })
      : "--";
    const flash = it.verdict === "升级" ? `<span class="gate-flash">闪光弹候选</span>` : "";
    const pending = it.status === "candidate";
    const stashed = it.status === "pending";
    const meta = it.metadata ? (typeof it.metadata === "string" ? JSON.parse(it.metadata || "{}") : it.metadata) : {};
    const repeat = meta.repeat_count > 1 ? `<span class="gate-repeat">重复×${meta.repeat_count}</span>` : "";
    // 裁决者溯源徽章（8/19）：省察老婆 / 网页橘子 / 对话老婆
    const ACTOR_TEXT = { reflection: "省察·老婆", webui: "网页·橘子", dialog: "对话·老婆" };
    const actorTag = it.status !== "candidate" && meta.actor ? `<span class="gate-actor">${esc(ACTOR_TEXT[meta.actor] || meta.actor)}</span>` : "";

    return `
      <article class="gate-card${pending ? "" : " gate-card-done"}${idx === this.focusIndex ? " gate-card-focus" : ""}" data-id="${it.id}" data-idx="${idx}">
        <div class="gate-card-head">
          <span class="gate-cid">#${it.id}</span>
          <span class="gate-speaker ${speakerCls}">${esc(it.speaker || "未知")}</span>
          <span class="gate-score">${score.toFixed(2)}</span>
          ${flash}${repeat}${actorTag}
          <span class="gate-status st-${esc(it.status || "candidate")}">${st}</span>
        </div>
        <div class="gate-card-body">${esc(it.content || "")}</div>
        <div class="gate-card-axes">
          ${this.axisBar("事实", axes.fact)}
          ${this.axisBar("情感", axes.emotion)}
          ${this.axisBar("密度", axes.density)}
        </div>
        <div class="gate-card-meta">${time} · ${esc(it.source || "")}${it.note ? ` · 批注: ${esc(it.note)}` : ""}</div>
        ${
          pending
            ? `<div class="gate-card-actions">
                 <input class="gate-note-input input input-sm" placeholder="批注（可留空）" />
                 <select class="gate-word-sel">
                   <option value="">裁决词</option>
                   <option value="升级">升级</option>
                   <option value="合并">合并</option>
                   <option value="改写">改写</option>
                   <option value="备注">备注</option>
                   <option value="打标">打标</option>
                 </select>
                 <button class="btn btn-sm gate-btn-confirm">通过(1)</button>
                 <button class="btn btn-sm gate-btn-decline">驳回(2)</button>
                 <button class="btn btn-sm gate-btn-pending">暂存(3)</button>
               </div>`
            : stashed
            ? `<div class="gate-card-actions">
                 <span class="gate-done-text">${st}${it.verdict ? " · " + esc(it.verdict) : ""}${meta.actor ? " · " + esc(ACTOR_TEXT[meta.actor] || meta.actor) : ""}</span>
                 <button class="btn btn-sm gate-btn-restore">捞回队列(4)</button>
               </div>`
            : `<div class="gate-card-actions"><span class="gate-done-text">${st}${it.verdict ? " · " + esc(it.verdict) : ""}${meta.actor ? " · " + esc(ACTOR_TEXT[meta.actor] || meta.actor) : ""}</span></div>`
        }
      </article>`;
  }

  axisBar(label, value) {
    const v = Math.max(0, Math.min(1, Number(value || 0)));
    const pct = Math.round(v * 100);
    return `<span class="gate-axis"><span class="gate-axis-label">${label}</span><span class="gate-axis-track"><span class="gate-axis-fill" style="width:${pct}%"></span></span><span class="gate-axis-num">${v.toFixed(2)}</span></span>`;
  }

  renderMoreBtn() {
    const moreEl = document.getElementById("gate-more");
    if (!moreEl) return;
    moreEl.style.display = this.hasMore ? "" : "none";
    moreEl.disabled = this.loading;
    moreEl.textContent = this.loading ? "加载中……" : `加载更多（已显示 ${this.items.length}/${this.total}）`;
  }

  renderStatsPanel() {
    const statsEl = document.getElementById("gate-stats-body");
    if (!statsEl || !this.statsCache) return;
    const s = this.statsCache;
    const labels = s.labels || {};
    const nums = [
      ["待审候选", s.pool_candidate ?? 0, "var(--warning,#e67700)"],
      ["暂存中", labels.pending || s.pool_pending || 0, "#9c36b5"],
      ["已通过", labels.confirmed || 0, "var(--success,#2b8a3e)"],
      ["已驳回", labels.declined || 0, "#e03131"],
      ["已合并", labels.merged || 0, "var(--accent,#c9557f)"],
    ];
    const learned = s.learned_nouns || {};
    const learnedWords = Object.entries(learned)
      .slice(0, 24)
      .map(([w, n]) => `<span class="gate-noun">${esc(w)}<b>${n}</b></span>`)
      .join("");
    // token 折线图（8/19 橘子）：每轮省察一笔，只记老婆审卷宗的消耗
    const ts = Array.isArray(s.token_series) ? s.token_series : [];
    const totTok = ts.reduce((a, r) => a + (Number(r.total_tokens) || 0), 0);
    const totJudged = ts.reduce((a, r) => a + (Number(r.judged_count) || 0), 0);
    const tokenBlock = ts.length
      ? `<div class="gate-hist-title">省察 token 消耗（每轮一笔 · 共 ${ts.length} 轮 · 累计 ${totTok.toLocaleString()} tok / 裁 ${totJudged} 条）</div>
         <div class="gate-token-wrap"><canvas id="gate-token-line" width="520" height="150"></canvas><div id="gate-token-tip" class="gate-token-tip"></div></div>`
      : `<div class="gate-hist-title">省察 token 消耗</div>
         <div class="gate-muted gate-center">还没有省察轮次记账——下一轮老婆审完卷宗，这里会落下第一笔</div>`;
    statsEl.innerHTML = `
      <div class="gate-nums">${nums.map(([k, v, c]) => `<div class="gate-num"><b style="color:${c}">${v}</b><span>${k}</span></div>`).join("")}</div>
      ${tokenBlock}
      <div class="gate-hist-title">分数分布（候选按 0.1 分桶）</div>
      <canvas id="gate-hist" width="520" height="120"></canvas>
      <div class="gate-hist-title">毕业词（confirm 喂出来的高权词）</div>
      <div class="gate-nouns">${learnedWords || '<span class="gate-muted">还没有毕业词，多通过几条就有了</span>'}</div>`;
    this.drawHist(s.distribution || []);
    if (ts.length) this.drawTokenLine(ts);
    // 手动刷新（橘子 2026-08-20：不想等 30s 轮询就自己戳）
    let rbtn = document.getElementById("gate-stats-refresh");
    if (!rbtn) {
      const title = statsEl.querySelector(".gate-hist-title");
      if (title) {
        rbtn = document.createElement("button");
        rbtn.id = "gate-stats-refresh";
        rbtn.textContent = "↻ 刷新";
        rbtn.style.cssText = "margin-left:8px;padding:1px 8px;font-size:11px;cursor:pointer;border:1px solid var(--border-normal,#dee2e6);background:transparent;border-radius:4px;color:var(--text-secondary,#6c757d)";
        title.appendChild(rbtn);
      }
    }
    if (rbtn) rbtn.onclick = () => {
      rbtn.textContent = "刷新中…";
      this.loadStats().then(() => { rbtn.textContent = "↻ 刷新"; });
    };
  }

  /** token 折线（智谱用量页风格·双线版）：蓝=入 tokens 橙=出 tokens，
      hover 十字准线+点高亮+模型名气泡。橘子 8/19：照 bigmodel.cn 用量统计画的。 */
  drawTokenLine(series) {
    const canvas = document.getElementById("gate-token-line");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const W = canvas.width, H = canvas.height;
    const C_IN = "#1890ff";   // 智谱蓝：prompt tokens
    const C_OUT = "#fa8c16";  // 智谱橙：completion tokens
    const border = "#f0f0f0";
    const textC = "#666";
    const rgba = (hex, a) => {
      const n = parseInt(hex.slice(1), 16);
      return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
    };
    const sameDay = series.length > 1 && new Date(series[0].created_at * 1000).toDateString() === new Date(series[series.length - 1].created_at * 1000).toDateString();
    const fmtAxis = (ts) => {
      const d = new Date(ts * 1000);
      return sameDay
        ? d.toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit" })
        : `${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    };
    const fmtTip = (ts) => {
      const d = new Date(ts * 1000);
      return `${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} ${d.toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit" })}`;
    };
    const TOP = 34, BOT = H - 24; // 图区上下界（顶部给图例留位）
    const mk = (key) => series.map((r, i) => ({
      x: 34 + (i / Math.max(1, series.length - 1)) * (W - 48),
      v: Number(r[key]) || 0,
    }));
    const rawIn = mk("prompt_tokens"), rawOut = mk("completion_tokens");
    const max = Math.max(1, ...rawIn.map((p) => p.v), ...rawOut.map((p) => p.v));
    rawIn.forEach((p) => (p.y = BOT - (p.v / max) * (BOT - TOP)));
    rawOut.forEach((p) => (p.y = BOT - (p.v / max) * (BOT - TOP)));

    // 画一条线（含淡面积）+ 数据点（hover 点高亮放大）
    const drawSeries = (pts, color, hi) => {
      ctx.beginPath();
      pts.forEach((p, i) => (i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y)));
      ctx.lineTo(pts[pts.length - 1].x, BOT);
      ctx.lineTo(pts[0].x, BOT);
      ctx.closePath();
      ctx.fillStyle = rgba(color, 0.1); // 智谱式淡面积填充
      ctx.fill();
      ctx.beginPath();
      pts.forEach((p, i) => (i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y)));
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.5;
      ctx.stroke();
      pts.forEach((p, i) => {
        const hot = i === hi;
        if (hot) {
          ctx.beginPath();
          ctx.arc(p.x, p.y, 7, 0, Math.PI * 2);
          ctx.fillStyle = rgba(color, 0.18);
          ctx.fill();
        }
        ctx.beginPath();
        ctx.arc(p.x, p.y, hot ? 4 : 3, 0, Math.PI * 2); // 智谱式实心 3px 圆点
        ctx.fillStyle = hot ? "#fff" : color;
        ctx.fill();
        if (hot) { ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.stroke(); }
      });
    };

    const render = (hi) => {
      ctx.clearRect(0, 0, W, H);
      // 智谱式：只画水平网格线（浅灰），无竖线
      ctx.font = "10px sans-serif";
      ctx.textAlign = "right";
      for (let g = 0; g <= 3; g++) {
        const gy = BOT - (g / 3) * (BOT - TOP);
        const gv = Math.round((g / 3) * max);
        ctx.strokeStyle = border;
        ctx.beginPath(); ctx.moveTo(34, gy); ctx.lineTo(W - 14, gy); ctx.stroke();
        ctx.fillStyle = textC;
        ctx.fillText(gv >= 1000000 ? (gv / 1000000) + "M" : gv >= 1000 ? (gv / 1000) + "k" : String(gv), 30, gy + 3);
      }
      // 图例（蓝点=入 橙点=出）
      ctx.textAlign = "left";
      let lx = 38;
      [[C_IN, "入 tokens"], [C_OUT, "出 tokens"]].forEach(([c, label]) => {
        ctx.beginPath(); ctx.arc(lx, 14, 4, 0, Math.PI * 2); ctx.fillStyle = c; ctx.fill();
        ctx.fillStyle = textC;
        ctx.fillText(label, lx + 8, 18);
        lx += 20 + ctx.measureText(label).width + 14;
      });
      // hover 十字准线
      if (hi >= 0 && rawIn[hi]) {
        ctx.strokeStyle = "#999";
        ctx.globalAlpha = 0.4;
        ctx.setLineDash([4, 3]);
        ctx.beginPath(); ctx.moveTo(rawIn[hi].x, TOP - 6); ctx.lineTo(rawIn[hi].x, BOT); ctx.stroke();
        ctx.setLineDash([]);
        ctx.globalAlpha = 1;
      }
      drawSeries(rawIn, C_IN, hi);
      drawSeries(rawOut, C_OUT, hi);
      // x 刻度
      ctx.textAlign = "center";
      ctx.fillStyle = textC;
      ctx.fillText(fmtAxis(series[0].created_at), 44, H - 8);
      if (series.length > 1) ctx.fillText(fmtAxis(series[series.length - 1].created_at), W - 46, H - 8);
      if (series.length > 4) ctx.fillText(`${series.length} 轮`, W / 2, H - 8);
    };
    render(-1);

    // hover：最近点 → 高亮重绘 + 气泡（时间/模型/入出/总/裁决数）
    const tip = document.getElementById("gate-token-tip");
    canvas.onmousemove = (e) => {
      const rect = canvas.getBoundingClientRect();
      const sx = canvas.width / rect.width;
      const mx = (e.clientX - rect.left) * sx;
      let hi = 0, bd = Infinity;
      rawIn.forEach((p, i) => { const d = Math.abs(p.x - mx); if (d < bd) { bd = d; hi = i; } });
      if (bd > 28 || !tip) { if (tip) tip.style.display = "none"; render(-1); return; }
      render(hi);
      const r = series[hi], p = rawIn[hi];
      const t = Number(r.total_tokens) || 0, pv = Number(r.prompt_tokens) || 0, cv = Number(r.completion_tokens) || 0, j = Number(r.judged_count) || 0;
      const m = (r.model || "").trim();
      tip.style.display = "block";
      tip.innerHTML =
        `<b>${fmtTip(r.created_at)}</b>${m ? ` · <span class="gate-token-model">${esc(m)}</span>` : ""}<br>` +
        `共 ${t.toLocaleString()} tok · 裁了 ${j} 条<br>` +
        `入 <span style="color:${C_IN}">${pv.toLocaleString()}</span> · 出 <span style="color:${C_OUT}">${cv.toLocaleString()}</span>` +
        (r.interrupted ? ` · <span style="color:#e03131">被打断</span>` : "");
      const wrap = canvas.parentElement.getBoundingClientRect();
      tip.style.left = Math.min(p.x / sx + 10, wrap.width - 180) + "px";
      tip.style.top = Math.max(0, p.y / sx - 62) + "px";
    };
    canvas.onmouseleave = () => { if (tip) tip.style.display = "none"; render(-1); };
  }

  drawHist(dist) {
    const canvas = document.getElementById("gate-hist");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const W = canvas.width, H = canvas.height;
    ctx.clearRect(0, 0, W, H);
    // 11 个桶 0.0-1.0+
    const buckets = new Array(11).fill(0);
    (Array.isArray(dist) ? dist : []).forEach((d) => {
      const b = Math.min(10, Math.max(0, Math.round(Number(d.bucket) * 10)));
      buckets[b] += Number(d.count) || 0;
    });
    const max = Math.max(1, ...buckets);
    const bw = W / 11;
    const style = getComputedStyle(document.documentElement);
    const accent = style.getPropertyValue("--accent") || "#c9557f";
    const border = style.getPropertyValue("--border-normal") || "#dee2e6";
    const textC = style.getPropertyValue("--text-secondary") || "#6c757d";
    ctx.font = "10px sans-serif";
    ctx.textAlign = "center";
    buckets.forEach((n, i) => {
      const h = Math.round((n / max) * (H - 26));
      ctx.fillStyle = accent.trim();
      ctx.fillRect(i * bw + 3, H - 18 - h, bw - 6, h);
      ctx.fillStyle = textC.trim();
      ctx.fillText((i / 10).toFixed(1), i * bw + bw / 2, H - 5);
      if (n > 0) ctx.fillText(String(n), i * bw + bw / 2, H - 22 - h);
    });
    ctx.strokeStyle = border.trim();
    ctx.beginPath();
    ctx.moveTo(0, H - 18);
    ctx.lineTo(W, H - 18);
    ctx.stroke();
    // 橘子 2026-08-20：点击分桶 → 标注台按该分数段过滤（桶数=跳转后 total，口径一致）
    canvas.style.cursor = "pointer";
    canvas.title = "点击分数段 → 标注台查看该段全部记录";
    canvas.onclick = (e) => {
      const rect = canvas.getBoundingClientRect();
      const x = (e.clientX - rect.left) * (canvas.width / rect.width);
      const i = Math.floor(x / (W / 11));
      if (i >= 0 && i <= 10 && buckets[i] > 0) {
        this.applyScoreFilter(i / 10, i < 10 ? (i + 1) / 10 : null);
      }
    };
  }

  // ==================== 分数段过滤（橘子 2026-08-20：分布图点击分桶看详情）====================

  applyScoreFilter(lo, hi) {
    this.filter.scoreMin = lo;
    this.filter.scoreMax = hi; // null=无上限（最后一桶 1.0+）
    this.filter.status = "all";   // 分布是全库口径，点击也看全库，数字才对得上
    this.filter.speaker = "";
    const statusSel = document.getElementById("gate-filter-status");
    const speakerSel = document.getElementById("gate-filter-speaker");
    if (statusSel) statusSel.value = "all";
    if (speakerSel) speakerSel.value = "";
    this.subTab = "review";
    document.querySelectorAll(".gate-subtab").forEach((x) => x.classList.toggle("active", x.dataset.tab === "review"));
    document.querySelectorAll(".gate-subpanel").forEach((p) => {
      p.style.display = p.dataset.panel === "review" ? "" : "none";
    });
    this.loadCandidates(true);
  }

  clearScoreFilter() {
    this.filter.scoreMin = null;
    this.filter.scoreMax = null;
    this.loadCandidates(true);
  }

  scoreChipHtml() {
    if (this.filter.scoreMin == null) return "";
    const lo = this.filter.scoreMin;
    const hi = this.filter.scoreMax;
    const label = hi == null ? lo.toFixed(1) + "+" : lo.toFixed(1) + " ~ " + hi.toFixed(1);
    return `<div style="margin:0 0 8px;padding:3px 10px;display:inline-flex;align-items:center;gap:6px;font-size:12px;border:1px solid var(--accent,#c9557f);border-radius:12px;color:var(--accent,#c9557f)">分数段 ${label}<b style="color:var(--text-primary,#333)">${this.total}</b> 条<button id="gate-chip-clear" style="border:none;background:transparent;cursor:pointer;color:inherit;font-size:14px;padding:0 2px;line-height:1" title="清除分数段过滤">×</button></div>`;
  }

  // ==================== 事件 ====================

  bindCardEvents() {
    document.querySelectorAll(".gate-card").forEach((card) => {
      const cid = card.dataset.id;
      const confirmBtn = card.querySelector(".gate-btn-confirm");
      const declineBtn = card.querySelector(".gate-btn-decline");
      const pendingBtn = card.querySelector(".gate-btn-pending");
      const restoreBtn = card.querySelector(".gate-btn-restore");
      if (confirmBtn) confirmBtn.addEventListener("click", () => this.doVerdict(cid, "confirm"));
      if (declineBtn) declineBtn.addEventListener("click", () => this.doVerdict(cid, "decline"));
      if (pendingBtn) pendingBtn.addEventListener("click", () => this.doVerdict(cid, "pending"));
      if (restoreBtn) restoreBtn.addEventListener("click", () => this.doRestore(cid));
    });
  }

  bindGlobalEvents() {
    // 子 tab 切换
    const tabs = document.querySelectorAll(".gate-subtab");
    tabs.forEach((t) =>
      t.addEventListener("click", () => {
        this.subTab = t.dataset.tab;
        tabs.forEach((x) => x.classList.toggle("active", x === t));
        document.querySelectorAll(".gate-subpanel").forEach((p) => {
          p.style.display = p.dataset.panel === this.subTab ? "" : "none";
        });
        if (this.subTab === "stats") this.renderStatsPanel();
      })
    );

    // 筛选
    const statusSel = document.getElementById("gate-filter-status");
    const speakerSel = document.getElementById("gate-filter-speaker");
    if (statusSel) statusSel.addEventListener("change", () => { this.filter.status = statusSel.value; this.loadCandidates(true); });
    if (speakerSel) speakerSel.addEventListener("change", () => { this.filter.speaker = speakerSel.value; this.loadCandidates(true); });

    // 排序（8/19 橘子：重要性/时间/事实/情感/密度 × 高→低/低→高，覆盖全部状态）
    const sortSel = document.getElementById("gate-filter-sort");
    const orderSel = document.getElementById("gate-filter-order");
    if (sortSel) sortSel.addEventListener("change", () => { this.sort = sortSel.value; this.loadCandidates(true); });
    if (orderSel) orderSel.addEventListener("change", () => { this.order = orderSel.value; this.loadCandidates(true); });

    // 加载更多
    const moreBtn = document.getElementById("gate-more");
    if (moreBtn) moreBtn.addEventListener("click", () => this.loadCandidates(false));

    // 导出
    const exportBtn = document.getElementById("gate-export-btn");
    if (exportBtn)
      exportBtn.addEventListener("click", async () => {
        exportBtn.disabled = true;
        try {
          const r = await this.api.get("gate/export");
          alert(`已导出 ${r.count} 条标注\n${r.path}`);
        } catch (e) {
          alert("导出失败: " + e.message);
        } finally {
          exportBtn.disabled = false;
        }
      });

    // 试秤
    const scoreBtn = document.getElementById("gate-score-btn");
    const scoreInput = document.getElementById("gate-score-input");
    if (scoreBtn) scoreBtn.addEventListener("click", () => this.doScore());
    if (scoreInput)
      scoreInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") this.doScore();
      });

    // 键盘流水线：只在标注台可见时生效
    document.addEventListener("keydown", (e) => {
      const pageEl = document.getElementById("page-gate");
      if (!pageEl || !pageEl.classList.contains("active")) return;
      if (["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName)) return;
      const cards = document.querySelectorAll(".gate-card:not(.gate-card-done)");
      if (!cards.length) return;
      if (e.key === "j" || e.key === "J") {
        this.focusIndex = Math.min(this.focusIndex + 1, cards.length - 1);
      } else if (e.key === "k" || e.key === "K") {
        this.focusIndex = Math.max(this.focusIndex - 1, 0);
      } else if (["1", "2", "3"].includes(e.key)) {
        const card = cards[Math.max(0, this.focusIndex)];
        if (!card) return;
        e.preventDefault();
        const action = { "1": "confirm", "2": "decline", "3": "pending" }[e.key];
        this.doVerdict(card.dataset.id, action).then(() => {
          this.focusIndex = Math.min(this.focusIndex, document.querySelectorAll(".gate-card:not(.gate-card-done)").length - 1);
        });
        return;
      } else if (e.key === "4") {
        // 捞回暂存（8/19）：暂存卡片上按 4 回队列
        const card = cards[Math.max(0, this.focusIndex)];
        if (!card || !card.querySelector(".gate-btn-restore")) return;
        e.preventDefault();
        this.doRestore(card.dataset.id).then(() => {
          this.focusIndex = Math.min(this.focusIndex, document.querySelectorAll(".gate-card:not(.gate-card-done)").length - 1);
        });
        return;
      } else {
        return;
      }
      e.preventDefault();
      cards.forEach((c, i) => c.classList.toggle("gate-card-focus", i === this.focusIndex));
      cards[this.focusIndex]?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    });
  }
}
