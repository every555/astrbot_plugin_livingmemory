/**
 * Trace Page - Context 组装追踪页面
 * 展示每次 LLM 请求的完整记忆注入链路
 */

import { esc } from "./utils.js";

export class TracePage {
  constructor(state, apiClient) {
    this.state = state;
    this.api = apiClient;
    this._expandedTraces = new Set();
  }

  /**
   * 初始化事件监听
   */
  initEventListeners() {
    const refreshBtn = document.getElementById("trace-refresh-btn");
    if (refreshBtn) {
      refreshBtn.addEventListener("click", () => this.fetch());
    }

    const limitSelect = document.getElementById("trace-limit");
    if (limitSelect) {
      limitSelect.addEventListener("change", () => this.fetch());
    }
  }

  /**
   * 获取 Trace 列表
   */
  async fetch() {
    const listEl = document.getElementById("trace-list");
    const statsEl = document.getElementById("trace-stats");
    const refreshBtn = document.getElementById("trace-refresh-btn");

    if (refreshBtn) refreshBtn.disabled = true;
    if (listEl) listEl.innerHTML = '<div class="trace-loading">' + window.t("common.loading") + '</div>';

    try {
      const limitSelect = document.getElementById("trace-limit");
      const limit = limitSelect ? limitSelect.value : "20";
      const data = await this.api.get("trace/list", { limit });

      this.state._traceCache = data;

      // 渲染统计
      this.renderStats(data.stats || {});

      // 渲染列表
      this.renderList(data.traces || []);
    } catch (e) {
      if (listEl) listEl.innerHTML = '<div class="trace-error">' + esc(e.message || window.t("misc.requestFailed")) + '</div>';
      if (statsEl) statsEl.classList.add("hidden");
    } finally {
      if (refreshBtn) refreshBtn.disabled = false;
    }
  }

  /**
   * 渲染统计卡片
   */
  renderStats(stats) {
    const statsEl = document.getElementById("trace-stats");
    if (!statsEl) return;

    if (!stats || !stats.total_traces) {
      statsEl.classList.add("hidden");
      return;
    }

    statsEl.classList.remove("hidden");

    const items = [
      { label: window.t("trace.totalTraces"), value: stats.total_traces || 0 },
      { label: window.t("trace.uniqueSessions"), value: stats.unique_sessions || 0 },
      { label: window.t("trace.avgInjected"), value: stats.avg_injected || 0 },
      { label: window.t("trace.maxInjected"), value: stats.max_injected || 0 },
      { label: window.t("trace.skippedCount"), value: stats.skipped_count || 0 },
    ];

    statsEl.innerHTML = items.map(item =>
      '<div class="trace-stat-card">' +
      '<span class="trace-stat-value">' + esc(String(item.value)) + '</span>' +
      '<span class="trace-stat-label">' + esc(item.label) + '</span>' +
      '</div>'
    ).join("");
  }

  /**
   * 渲染 Trace 列表
   */
  renderList(traces) {
    const listEl = document.getElementById("trace-list");
    if (!listEl) return;

    if (!traces.length) {
      listEl.innerHTML = '<div class="trace-empty">' + window.t("trace.noTraces") + '</div>';
      return;
    }

    let html = '<div class="trace-list-container">';

    traces.forEach((trace) => {
      const traceId = trace.trace_id || "";
      const isExpanded = this._expandedTraces.has(traceId);
      const ts = trace.timestamp
        ? new Date(trace.timestamp * 1000).toLocaleString()
        : "--";
      const skipped = trace.skipped;
      const emotion = trace.emotion_detected || "neutral";
      const emotionCls = ["happy", "excited", "tired", "sad", "angry"].includes(emotion) ? emotion : "neutral";
      const intent = trace.query_intent || "default";

      html += '<div class="trace-item' + (isExpanded ? ' expanded' : '') + '" data-trace-id="' + esc(traceId) + '">';

      // 摘要行
      html += '<div class="trace-item-header" data-trace-toggle="' + esc(traceId) + '">';
      html += '<div class="trace-item-main">';
      html += '<span class="trace-time">' + esc(ts) + '</span>';
      html += '<span class="trace-query">' + esc(trace.query_raw || "(empty query)") + '</span>';
      html += '</div>';
      html += '<div class="trace-item-badges">';

      if (skipped) {
        html += '<span class="trace-badge skipped">' + window.t("trace.skipped") + '</span>';
      } else {
        html += '<span class="trace-badge injected">' + trace.injected_count + ' ' + window.t("trace.injected") + '</span>';
      }

      if (trace.summary_injected) {
        html += '<span class="trace-badge summary">' + window.t("trace.summary") + '</span>';
      }

      if (trace.stream_atoms_extracted > 0) {
        html += '<span class="trace-badge atoms">' + trace.stream_atoms_extracted + ' ' + window.t("trace.atoms") + '</span>';
      }

      html += '<span class="trace-badge emotion ' + emotionCls + '">' + esc(emotion) + '</span>';
      html += '<span class="trace-badge intent">' + esc(intent) + '</span>';
      html += '</div>';
      html += '<span class="trace-expand-icon">' + (isExpanded ? "▾" : "▸") + '</span>';
      html += '</div>';

      // 展开内容
      if (isExpanded) {
        html += this.renderDetailPlaceholder(traceId);
      }

      html += '</div>';
    });

    html += '</div>';
    listEl.innerHTML = html;

    // 绑定点击展开/折叠
    listEl.querySelectorAll("[data-trace-toggle]").forEach(header => {
      header.addEventListener("click", (e) => {
        const id = header.dataset.traceToggle;
        if (this._expandedTraces.has(id)) {
          this._expandedTraces.delete(id);
        } else {
          this._expandedTraces.add(id);
        }
        this.renderList(traces);
      });
    });

    // 加载已展开的详情
    this._expandedTraces.forEach(id => {
      if (traces.find(t => t.trace_id === id)) {
        this.loadTraceDetail(id);
      }
    });
  }

  /**
   * 渲染详情占位符
   */
  renderDetailPlaceholder(traceId) {
    return '<div class="trace-detail" id="trace-detail-' + esc(traceId) + '">' +
      '<div class="trace-loading">' + window.t("common.loading") + '</div>' +
      '</div>';
  }

  /**
   * 加载单条 Trace 详情
   */
  async loadTraceDetail(traceId) {
    const detailEl = document.getElementById("trace-detail-" + traceId);
    if (!detailEl) return;

    try {
      const data = await this.api.get("trace/detail", { trace_id: traceId });
      detailEl.innerHTML = this.renderDetail(data);
    } catch (e) {
      detailEl.innerHTML = '<div class="trace-error">' + esc(e.message || "Failed to load") + '</div>';
    }
  }

  /**
   * 渲染详情内容
   */
  renderDetail(trace) {
    let html = '<div class="trace-detail-content">';

    // ── 基本信息时间线 ──
    html += '<div class="trace-section">';
    html += '<h4 class="trace-section-title">' + window.t("trace.sectionBasic") + '</h4>';
    html += '<div class="trace-meta-grid">';

    const metaItems = [
      { label: "Trace ID", value: trace.trace_id },
      { label: window.t("trace.sessionId"), value: trace.session_id || "--" },
      { label: window.t("trace.queryIntent"), value: trace.query_intent || "default" },
      { label: window.t("trace.emotion"), value: trace.emotion_detected || "neutral" },
      { label: window.t("trace.contextExpanded"), value: trace.context_expanded_count || 0 },
      { label: window.t("trace.injectionMethod"), value: trace.injection_method || "--" },
    ];

    if (trace.injection_fallback) {
      metaItems.push({ label: window.t("trace.fallbackReason"), value: trace.injection_fallback });
    }
    if (trace.injected_tokens_est > 0) {
      metaItems.push({ label: window.t("trace.estTokens"), value: "~" + trace.injected_tokens_est });
    }

    metaItems.forEach(item => {
      html += '<div class="trace-meta-item">';
      html += '<span class="trace-meta-label">' + esc(item.label) + '</span>';
      html += '<span class="trace-meta-value">' + esc(String(item.value)) + '</span>';
      html += '</div>';
    });

    html += '</div>';

    // 扩展查询
    if (trace.query_expanded) {
      html += '<div class="trace-query-expanded">';
      html += '<span class="trace-meta-label">' + window.t("trace.expandedQuery") + '</span>';
      html += '<div class="trace-query-text">' + esc(trace.query_expanded) + '</div>';
      html += '</div>';
    }

    html += '</div>';

    // ── 路由权重 ──
    if (trace.route_weights && Object.keys(trace.route_weights).length) {
      html += '<div class="trace-section">';
      html += '<h4 class="trace-section-title">' + window.t("trace.sectionRouting") + '</h4>';
      html += '<div class="trace-route-weights">';
      const docW = trace.route_weights.document || 0;
      const graphW = trace.route_weights.graph || 0;
      html += '<div class="route-weight-bar">';
      html += '<div class="route-weight-doc" style="width: ' + (docW * 100) + '%" title="Document: ' + (docW * 100).toFixed(0) + '%"></div>';
      html += '<div class="route-weight-graph" style="width: ' + (graphW * 100) + '%" title="Graph: ' + (graphW * 100).toFixed(0) + '%"></div>';
      html += '</div>';
      html += '<div class="route-weight-labels">';
      html += '<span>' + window.t("trace.docRoute") + ': ' + (docW * 100).toFixed(0) + '% (' + (trace.doc_route_count || 0) + ' ' + window.t("trace.results") + ')</span>';
      html += '<span>' + window.t("trace.graphRoute") + ': ' + (graphW * 100).toFixed(0) + '% (' + (trace.graph_route_count || 0) + ' ' + window.t("trace.results") + ')</span>';
      html += '</div>';
      html += '</div>';
      html += '</div>';
    }

    // ── 融合结果 ──
    if (trace.merged_results && trace.merged_results.length) {
      html += '<div class="trace-section">';
      html += '<h4 class="trace-section-title">' + window.t("trace.sectionMerged") + ' (' + trace.merged_results.length + ')</h4>';
      html += '<div class="trace-merged-list">';

      trace.merged_results.forEach((r, idx) => {
        const score = r.final_score != null ? Number(r.final_score).toFixed(3) : "--";
        const scoreNum = Number(score);
        const scoreCls = scoreNum >= 0.75 ? "high" : scoreNum >= 0.45 ? "medium" : "low";

        html += '<div class="trace-merged-item">';
        html += '<span class="trace-rank">#' + (idx + 1) + '</span>';
        html += '<span class="trace-merged-id">ID: ' + esc(String(r.doc_id)) + '</span>';
        html += '<span class="trace-score-badge ' + scoreCls + '">' + score + '</span>';
        html += '<div class="trace-merged-preview">' + esc(r.content_preview || "") + '</div>';

        // score breakdown
        if (r.score_breakdown && Object.keys(r.score_breakdown).length) {
          html += '<div class="trace-score-breakdown">';
          for (const [k, v] of Object.entries(r.score_breakdown)) {
            html += '<span class="score-part">' + esc(k) + ': ' + Number(v).toFixed(3) + '</span>';
          }
          html += '</div>';
        }

        html += '</div>';
      });

      html += '</div>';
      html += '</div>';
    }

    // ── 情感路由 ──
    if (trace.emotion_boost_applied > 0 || (trace.emotion_boost_details && trace.emotion_boost_details.length)) {
      html += '<div class="trace-section">';
      html += '<h4 class="trace-section-title">' + window.t("trace.sectionEmotion") + '</h4>';
      html += '<p class="trace-section-desc">' + window.t("trace.emotionDesc", trace.emotion_boost_applied) + '</p>';
      html += '</div>';
    }

    // ── 活跃窗口 ──
    if (trace.recency_boosted_count > 0) {
      html += '<div class="trace-section">';
      html += '<h4 class="trace-section-title">' + window.t("trace.sectionRecency") + '</h4>';
      html += '<p class="trace-section-desc">' + window.t("trace.recencyDesc", trace.recency_boosted_count) + '</p>';
      html += '</div>';
    }

    // ── 会话摘要 ──
    if (trace.summary_injected) {
      html += '<div class="trace-section">';
      html += '<h4 class="trace-section-title">' + window.t("trace.sectionSummary") + '</h4>';
      html += '<div class="trace-summary-text">' + esc(trace.summary_text || "") + '</div>';
      html += '</div>';
    }

    // ── 注入文本 ──
    if (trace.injected_text) {
      html += '<div class="trace-section">';
      html += '<h4 class="trace-section-title">' + window.t("trace.sectionInjection") + '</h4>';
      html += '<pre class="trace-injected-text">' + esc(trace.injected_text) + '</pre>';
      html += '</div>';
    }

    // ── 自省 ──
    if (trace.self_reflection) {
      html += '<div class="trace-section trace-reflection">';
      html += '<h4 class="trace-section-title">' + window.t("trace.sectionReflection") + '</h4>';
      html += '<div class="trace-reflection-text">' + esc(trace.self_reflection) + '</div>';
      html += '</div>';
    }

    // ── 错误 ──
    if (trace.errors && trace.errors.length) {
      html += '<div class="trace-section trace-errors">';
      html += '<h4 class="trace-section-title">' + window.t("trace.sectionErrors") + '</h4>';
      html += '<ul class="trace-error-list">';
      trace.errors.forEach(err => {
        html += '<li>' + esc(String(err)) + '</li>';
      });
      html += '</ul>';
      html += '</div>';
    }

    // ── 跳过原因 ──
    if (trace.skipped && trace.skip_reason) {
      html += '<div class="trace-section trace-skipped-reason">';
      html += '<h4 class="trace-section-title">' + window.t("trace.skipReason") + '</h4>';
      html += '<p>' + esc(trace.skip_reason) + '</p>';
      html += '</div>';
    }

    html += '</div>'; // .trace-detail-content
    return html;
  }

  /**
   * 显示 Toast
   */
  showToast(message, isError = false) {
    if (window.lmShowToast) {
      window.lmShowToast(message, isError);
    }
  }
}
