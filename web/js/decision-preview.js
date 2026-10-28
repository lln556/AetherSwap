/* Strategy decision preview: read real checks, explain outcomes, and compare
 * temporary existing parameters against the same saved data version. */
(() => {
  "use strict";

  const STATUS_LABELS = {
    pass: "通过",
    reject: "拒绝",
    wait: "等待",
    unavailable: "数据不可用",
    unsupported: "不支持",
    disabled: "未启用",
    not_run: "未执行",
    action: "预演跳过",
    running: "进行中",
    queued: "等待中",
    succeeded: "已完成",
    success: "已完成",
    failed: "失败",
    error: "失败",
    unknown: "未知",
  };

  const KIND_LABELS = {
    buy: "买入检查",
    sell: "普通上架",
    reprice: "自动调价",
  };

  const PARAM_LABELS = {
    max_discount: "成本率上限",
    history_days: "历史窗口（天）",
    sell_price_wall_volume: "价格墙数量",
    sell_price_max_ignore_volume: "忽略成交量",
    sell_price_offset: "价格偏移",
    max_listings: "同名在售上限",
    target_balance: "目标余额",
    min_profit: "最低利润",
  };

  const state = {
    initialized: false,
    listLoaded: false,
    listLoading: false,
    listRequestToken: 0,
    listError: "",
    items: [],
    total: 0,
    page: 1,
    pageSize: 20,
    retention: { days: 7, max_records: 10000 },
    recordingError: "",
    filters: { kind: "buy", name: "", status: "" },
    selectedRecordId: "",
    detailLoading: false,
    detail: null,
    selectedVersionId: "original",
    draftParams: {},
    preview: null,
    changedSteps: [],
    refreshBusy: false,
    refreshPollToken: 0,
    copyNotice: "",
    detailRequestToken: 0,
    simulationRequestToken: 0,
  };

  function byId(id) {
    return document.getElementById(id);
  }

  function normalizeStatus(value) {
    const raw = String(value || "unknown").trim().toLowerCase().replace(/[\s-]+/g, "_");
    if (["pass", "passed", "ok", "allow", "allowed"].includes(raw)) return "pass";
    if (["reject", "rejected", "blocked", "block"].includes(raw)) return "reject";
    if (["wait", "waiting", "pending", "hold"].includes(raw)) return "wait";
    if (["unavailable", "missing", "no_data", "data_unavailable", "unknown_data"].includes(raw)) return "unavailable";
    if (["unsupported", "not_supported"].includes(raw)) return "unsupported";
    if (["disabled", "not_enabled", "inactive"].includes(raw)) return "disabled";
    if (["not_run", "not_executed", "unexecuted", "skipped", "skip"].includes(raw)) return "not_run";
    if (["action", "dry_run"].includes(raw)) return "action";
    if (["running", "processing", "in_progress"].includes(raw)) return "running";
    if (["queued", "queued_for_refresh", "waiting_to_start"].includes(raw)) return "queued";
    if (["succeeded", "success", "completed", "complete", "done"].includes(raw)) return "succeeded";
    if (["failed", "failure", "error", "fail"].includes(raw)) return "failed";
    return raw || "unknown";
  }

  function statusLabel(value) {
    return STATUS_LABELS[normalizeStatus(value)] || String(value || "未知");
  }

  function statusBadge(value, extraClass = "") {
    const key = normalizeStatus(value);
    return `<span class="decision-status decision-status--${escapeHtml(key)} ${escapeHtml(extraClass)}" data-status="${escapeHtml(key)}">${escapeHtml(statusLabel(value))}</span>`;
  }

  function kindLabel(kind) {
    return KIND_LABELS[kind] || String(kind || "检查记录");
  }

  function formatDate(value) {
    if (!value) return "时间未知";
    const date = dateFromValue(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).format(date);
  }

  function fullDate(value) {
    if (!value) return "时间未知";
    const date = dateFromValue(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat("zh-CN", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(date);
  }

  function dateFromValue(value) {
    if (typeof value === "number" || (typeof value === "string" && /^\d+(?:\.\d+)?$/.test(value.trim()))) {
      const numeric = Number(value);
      if (Number.isFinite(numeric)) {
        return new Date(Math.abs(numeric) < 100000000000 ? numeric * 1000 : numeric);
      }
    }
    return new Date(value);
  }

  function formatNumber(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return String(value ?? "—");
    return number.toLocaleString("zh-CN", { maximumFractionDigits: 4 });
  }

  function isRatioContext(step, key = "") {
    const hint = `${key} ${step?.module_id || ""} ${step?.name || step?.module_name || ""}`.toLowerCase();
    if (/(discount|ratio|rate|percent|margin|prob)/.test(hint)) return true;
    return false;
  }

  function formatMetric(value, key = "", step = null) {
    if (value == null || value === "") return "—";
    if (typeof value === "boolean") return value ? "是" : "否";
    if (typeof value === "object") return JSON.stringify(value);
    const number = Number(value);
    if (Number.isFinite(number) && isRatioContext(step, key)) {
      return `${(number * 100).toFixed(2)}%`;
    }
    return formatNumber(value);
  }

  function valueLabel(value) {
    if (value == null || value === "") return "—";
    return typeof value === "object" ? JSON.stringify(value) : String(value);
  }

  function stepName(step, index) {
    return step?.module_name || step?.name || step?.label || state.detail?.record?.modules?.[step?.module_id]?.name || step?.module_id || `步骤 ${index + 1}`;
  }

  function stepReason(step) {
    return step?.reason || step?.message || step?.explanation || step?.detail || "此步骤没有提供额外说明。";
  }

  function stepStatus(step) {
    return normalizeStatus(step?.status || "unknown");
  }

  function renderSalePrice(steps) {
    const output = [...(steps || [])].reverse().find(step => step.output?.estimated_proceeds != null)?.output;
    if (!output) return "";
    const buyer = Number(output.display_price);
    const net = Number(output.estimated_proceeds);
    if (!Number.isFinite(buyer) || !Number.isFinite(net)) return "";
    const currency = escapeHtml(output.currency || "币种未标注");
    return `<div class="decision-step-metrics"><span><small>买家价格</small><b>${buyer.toFixed(2)} ${currency}</b></span><span><small>预计净到账</small><b>${net.toFixed(2)} ${currency}</b></span></div>`;
  }

  function renderStepList(steps) {
    if (!Array.isArray(steps) || !steps.length) {
      return `<div class="decision-inline-empty">本次记录没有保存步骤结果。</div>`;
    }
    const blocker = steps.find((step) => ["reject", "wait", "unavailable", "unsupported", "failed"].includes(stepStatus(step)));
    const summary = blocker ? `<div class="decision-first-blocker decision-inline-empty"><strong>首个阻断原因</strong><p>${escapeHtml(stepReason(blocker))}</p></div>` : "";
    return `${summary}${renderSalePrice(steps)}<div class="decision-step-list">${steps.map((step, index) => `
      <article class="decision-step decision-step--${escapeHtml(stepStatus(step))}">
        <div class="decision-step-head">
          <div>
            <span class="decision-step-index">${String(index + 1).padStart(2, "0")}</span>
            <strong>${escapeHtml(stepName(step, index))}</strong>
          </div>
          ${statusBadge(step?.status)}
        </div>
        <p class="decision-step-reason">${escapeHtml(stepReason(step))}</p>
        ${(step?.actual != null || step?.threshold != null || step?.value != null || step?.limit != null) ? `
          <div class="decision-step-metrics">
            ${step?.actual != null || step?.value != null ? `<span><small>实际值</small><b>${escapeHtml(formatMetric(step.actual ?? step.value, "actual", step))}</b></span>` : ""}
            ${step?.threshold != null || step?.limit != null ? `<span><small>阈值</small><b>${escapeHtml(formatMetric(step.threshold ?? step.limit, "threshold", step))}</b></span>` : ""}
          </div>
        ` : ""}
      </article>
    `).join("")}</div>`;
  }

  function setVisible(node, visible) {
    if (node) node.classList.toggle("hidden", !visible);
  }

  function buildQuery() {
    const query = new URLSearchParams();
    query.set("kind", state.filters.kind || "buy");
    if (state.filters.name.trim()) query.set("name", state.filters.name.trim());
    if (state.filters.status) query.set("status", state.filters.status);
    query.set("page", String(state.page));
    query.set("page_size", String(state.pageSize));
    return query;
  }

  function renderRetention() {
    const node = byId("decision-retention");
    if (!node) return;
    const days = Number(state.retention?.days || 7);
    const maxRecords = Number(state.retention?.max_records || 10000).toLocaleString("zh-CN");
    node.textContent = `检查记录保留最近 ${days} 天，最多 ${maxRecords} 条；超出后清理最旧记录。`;
  }

  function renderList() {
    const list = byId("decision-list");
    const empty = byId("decision-empty");
    const error = byId("decision-list-error");
    const summary = byId("decision-list-summary");
    const recordingWarning = byId("decision-recording-warning");
    const prev = byId("decision-page-prev");
    const next = byId("decision-page-next");
    const pageLabel = byId("decision-page-label");
    if (!list) return;
    list.setAttribute("aria-busy", state.listLoading ? "true" : "false");
    renderRetention();
    const failed = !!state.listError;
    setVisible(error, failed);
    if (error) error.textContent = failed ? `检查记录读取失败：${state.listError}` : "";
    setVisible(recordingWarning, !failed && !!state.recordingError);
    if (recordingWarning) {
      recordingWarning.textContent = !failed && state.recordingError
        ? `部分检查记录暂未保存：${state.recordingError}。正式交易判断不受影响。`
        : "";
    }
    if (state.listLoading && !state.items.length) {
      list.innerHTML = `<div class="decision-list-loading" role="status">正在读取检查记录…</div>`;
      setVisible(empty, false);
    } else if (failed) {
      list.innerHTML = "";
      setVisible(empty, false);
    } else if (!state.items.length) {
      list.innerHTML = "";
      setVisible(empty, true);
      if (empty) {
        empty.innerHTML = `
          <strong>还没有检查记录</strong>
          <p>真实检查记录从新功能启用后产生。没有进入逐件检查的商品不会在这里出现，也不会生成示例结果。</p>
          ${state.recordingError ? `<p class="decision-warning">记录暂不可用：${escapeHtml(state.recordingError)}</p>` : ""}
        `;
      }
    } else {
      setVisible(empty, false);
      list.innerHTML = state.items.map((item) => {
        const resultStatus = item?.result?.status;
        return `
          <button type="button" class="decision-record ${state.selectedRecordId === item.id ? "selected" : ""}" data-record-id="${escapeHtml(item.id)}">
            <span class="decision-record-top">
              <span class="decision-kind">${escapeHtml(kindLabel(item.kind))}</span>
              ${statusBadge(resultStatus)}
            </span>
            <strong>${escapeHtml(item?.item?.name || item?.item?.market_hash_name || "未知商品")}</strong>
            <small>${escapeHtml(item?.strategy?.name || item?.strategy?.id || "策略未知")} · ${escapeHtml(formatDate(item?.evaluated_at))}</small>
          </button>
        `;
      }).join("");
      list.querySelectorAll(".decision-record").forEach((node) => {
        node.addEventListener("click", () => openRecord(node.dataset.recordId));
      });
    }
    if (summary) {
      summary.textContent = state.listLoading
        ? "正在读取检查记录…"
        : failed
          ? "无法读取记录"
          : `共 ${Number(state.total || 0).toLocaleString("zh-CN")} 条检查记录`;
    }
    const pages = Math.max(1, Math.ceil(Number(state.total || 0) / state.pageSize));
    if (prev) prev.disabled = state.listLoading || state.page <= 1;
    if (next) next.disabled = state.listLoading || state.page >= pages;
    if (pageLabel) pageLabel.textContent = `第 ${state.page} / ${pages} 页`;
  }

  async function loadList(page = 1) {
    const token = ++state.listRequestToken;
    state.listLoading = true;
    state.page = Math.max(1, Number(page) || 1);
    state.listError = "";
    renderList();
    try {
      const query = buildQuery();
      const data = await fetchJson(`${API}/strategy-decisions?${query.toString()}`);
      if (token !== state.listRequestToken) return;
      if (!data.ok) throw new Error(data.error || "检查记录不可用");
      state.items = Array.isArray(data.items) ? data.items : [];
      state.total = Number(data.total || 0);
      state.page = Number(data.page || state.page);
      state.pageSize = Number(data.page_size || 20);
      state.retention = data.retention || state.retention;
      state.recordingError = data.recording_error ? String(data.recording_error) : "";
      state.listLoaded = true;
    } catch (error) {
      if (token !== state.listRequestToken) return;
      state.items = [];
      state.total = 0;
      state.listError = error.message || "检查记录不可用";
    } finally {
      if (token === state.listRequestToken) {
        state.listLoading = false;
        renderList();
      }
    }
  }

  function selectedVersion() {
    const versions = state.detail?.versions || [];
    return versions.find((version) => version.id === state.selectedVersionId) || null;
  }

  function selectedInputs() {
    return selectedVersion()?.inputs || state.detail?.record?.inputs || {};
  }

  function metadataValue(inputs, keys) {
    for (const key of keys) {
      const value = inputs?.[key];
      if (value == null || value === "") continue;
      if (typeof value === "object") {
        if (value.name || value.label || value.text) return value.name || value.label || value.text;
        continue;
      }
      return String(value);
    }
    return "";
  }

  function metadataValueFrom(inputsList, keys) {
    for (const inputs of inputsList) {
      const value = metadataValue(inputs, keys);
      if (value) return value;
    }
    return "";
  }

  function metadataWindow(inputsList) {
    for (const inputs of inputsList) {
      const days = inputs?.window_days;
      if (days != null && days !== "" && Number.isFinite(Number(days))) return `${Number(days)} 天`;
    }
    return metadataValueFrom(inputsList, ["window", "analysis_window", "history_window", "range", "period"]) || "未标注";
  }

  function inputMetadata(inputs) {
    const history = resolveHistory(inputs);
    const sources = [
      history.container,
      inputs?.market?.history,
      inputs?.trend,
      inputs?.market,
      inputs,
    ];
    return {
      source: metadataValueFrom(sources, ["source", "data_source", "market_source", "provider"]) || "记录内行情",
      collectedAt: metadataValueFrom(sources, ["collected_at", "fetched_at", "captured_at", "as_of", "timestamp"]),
      window: metadataWindow(sources),
      currency: metadataValueFrom(sources, ["currency", "price_currency", "market_currency"]) || "币种未标注",
    };
  }

  function renderVersionNote() {
    const version = selectedVersion();
    const node = byId("decision-version-note");
    if (!node) return;
    if (version) {
      node.textContent = `新数据版本 · 采集于 ${fullDate(version.created_at || version.evaluated_at)} · 原记录与原始解释保持不变。`;
    } else {
      node.textContent = `原始记录 · 评估于 ${fullDate(state.detail?.record?.evaluated_at)} · 固定输入，不随当前配置改变。`;
    }
  }

  function renderDataCard() {
    const version = selectedVersion();
    const inputs = selectedInputs();
    const meta = inputMetadata(inputs);
    const versionText = version ? `数据版本 ${fullDate(version.created_at || version.evaluated_at)}` : "原始检查数据";
    return `
      <section class="decision-card decision-data-card" id="decision-data-card">
        <div class="decision-card-head">
          <div><span class="strategy-label">数据版本</span><h4>${escapeHtml(versionText)}</h4></div>
          <span class="decision-version-chip" data-version-id="${escapeHtml(version?.id || "original")}">${version ? "新版本" : "原记录"}</span>
        </div>
        <dl class="decision-data-list">
          <div><dt>来源</dt><dd>${escapeHtml(meta.source)}</dd></div>
          <div><dt>采集时间</dt><dd>${escapeHtml(fullDate(meta.collectedAt))}</dd></div>
          <div><dt>分析窗口</dt><dd>${escapeHtml(meta.window)}</dd></div>
          <div><dt>币种</dt><dd>${escapeHtml(meta.currency)}</dd></div>
        </dl>
        <p class="decision-version-note" id="decision-data-version-note">输入固定为本次记录或所选数据版本，不读取当前行情。</p>
      </section>
    `;
  }

  function renderVersionControls(record) {
    if (record.kind === "reprice") return "";
    const versions = state.detail?.versions || [];
    const refresh = state.detail?.refresh;
    const refreshKey = normalizeStatus(refresh?.status || "");
    const refreshActive = state.refreshBusy || ["queued", "running", "processing"].includes(refreshKey);
    const options = [
      `<option value="original" ${state.selectedVersionId === "original" ? "selected" : ""}>原始记录（${escapeHtml(formatDate(record.evaluated_at))}）</option>`,
      ...versions.map((version, index) => `
        <option value="${escapeHtml(version.id)}" ${state.selectedVersionId === version.id ? "selected" : ""}>数据版本 ${index + 1}（${escapeHtml(formatDate(version.created_at || version.evaluated_at))}）</option>
      `),
    ].join("");
    let refreshText = "当前只有记录内数据。缺少数据时，可主动获取完整行情；刷新会创建新版本，不覆盖原始解释。";
    if (refreshActive) refreshText = refreshKey === "queued" ? "刷新任务已排队，正在等待执行…" : "正在获取完整行情并重新计算，请保持页面打开。";
    if (refreshKey === "succeeded") refreshText = "刷新已完成，已切换到新数据版本；原记录仍可查看。";
    if (refreshKey === "failed") refreshText = `刷新失败：${refresh.error || "未获得可用的完整行情"}。原始结果已保留。`;
    return `
      <section class="decision-card decision-version-card" id="decision-version-card">
        <div class="decision-card-head">
          <div><span class="strategy-label">比较数据版本</span><h4>固定输入，参数变化才影响预演</h4></div>
          <label class="decision-version-select"><span>数据版本</span><select id="decision-version">${options}</select></label>
        </div>
        <p id="decision-version-note" class="decision-version-note"></p>
        <div class="decision-refresh-row">
          <button type="button" class="btn btn-secondary btn-sm" id="decision-refresh" ${refreshActive ? "disabled" : ""}>${refreshActive ? "刷新中…" : "主动获取完整行情"}</button>
          <span id="decision-refresh-status" class="decision-refresh-status decision-refresh-status--${escapeHtml(refreshKey || "idle")}" role="status" aria-live="polite">
            ${escapeHtml(refreshText)}
          </span>
        </div>
        ${refreshActive ? `<div class="decision-refresh-progress" aria-label="刷新进行中"><span></span></div>` : ""}
      </section>
    `;
  }

  function parameterEntries(record) {
    return (record?.strategy?.steps || []).flatMap((step, stepIndex) => {
      if (step?.enabled === false) return [];
      return Object.entries(step?.params || {}).map(([key, value]) => ({ step, stepIndex, key, value }));
    });
  }

  function paramLabel(step, key, stepIndex) {
    const module = state.detail?.record?.modules?.[step.module_id];
    return `${PARAM_LABELS[key] || module?.params_schema?.[key]?.label || key} · ${step.name || module?.name || step.module_id || `步骤 ${stepIndex + 1}`}`;
  }

  function inputType(value) {
    if (typeof value === "boolean") return "boolean";
    if (typeof value === "number") return "number";
    if (typeof value === "object" && value !== null) return "json";
    return "text";
  }

  function draftValue(stepIndex, key, original) {
    const stepParams = state.draftParams?.[String(stepIndex)] || {};
    return Object.prototype.hasOwnProperty.call(stepParams, key) ? stepParams[key] : original;
  }

  function renderParamEditor(record) {
    if (!record || record.kind === "reprice") return "";
    const entries = parameterEntries(record);
    if (!entries.length) {
      return `<div class="decision-inline-empty">该策略没有可编辑的既有参数。</div>`;
    }
    return `<div class="decision-param-grid">${entries.map(({ step, stepIndex, key, value }) => {
      const current = draftValue(stepIndex, key, value);
      const type = inputType(value);
      const id = `decision-param-${stepIndex}-${key}`;
      if (type === "boolean") {
        return `
          <label class="decision-param decision-param--boolean" for="${escapeHtml(id)}">
            <span>${escapeHtml(paramLabel(step, key, stepIndex))}</span>
            <span class="decision-param-control"><input id="${escapeHtml(id)}" type="checkbox" data-preview-param-key="${escapeHtml(key)}" data-step-index="${stepIndex}" data-preview-param-type="boolean" ${current ? "checked" : ""} /><b>${current ? "启用" : "关闭"}</b></span>
            <small>原值：${escapeHtml(valueLabel(value))}</small>
          </label>
        `;
      }
      const rendered = type === "json" ? JSON.stringify(current) : current;
      return `
        <label class="decision-param" for="${escapeHtml(id)}">
          <span>${escapeHtml(paramLabel(step, key, stepIndex))}</span>
          ${type === "json"
            ? `<textarea id="${escapeHtml(id)}" rows="3" data-preview-param-key="${escapeHtml(key)}" data-step-index="${stepIndex}" data-preview-param-type="json">${escapeHtml(rendered)}</textarea>`
            : `<input id="${escapeHtml(id)}" type="${type}" step="any" value="${escapeHtml(rendered)}" data-preview-param-key="${escapeHtml(key)}" data-step-index="${stepIndex}" data-preview-param-type="${type}" />`}
          <small>原值：${escapeHtml(valueLabel(value))} · 只修改既有参数，不增删模块。</small>
        </label>
      `;
    }).join("")}</div>`;
  }

  function parseInputValue(input) {
    const type = input.dataset.previewParamType;
    if (type === "boolean") return input.checked;
    if (type === "number") {
      if (input.value.trim() === "") return "";
      const number = Number(input.value);
      return Number.isFinite(number) ? number : input.value;
    }
    if (type === "json") {
      try {
        return JSON.parse(input.value);
      } catch {
        return input.value;
      }
    }
    return input.value;
  }

  function captureDraftParams() {
    const params = {};
    document.querySelectorAll("[data-preview-param-key]").forEach((input) => {
      const stepIndex = String(input.dataset.stepIndex);
      const key = input.dataset.previewParamKey;
      const recordStep = state.detail?.record?.strategy?.steps?.[Number(stepIndex)];
      const original = recordStep?.params?.[key];
      const value = parseInputValue(input);
      if (JSON.stringify(value) === JSON.stringify(original)) return;
      params[stepIndex] = params[stepIndex] || {};
      params[stepIndex][key] = value;
    });
    state.draftParams = params;
    return params;
  }

  function strategyParamValue(strategy, stepIndex, key) {
    return strategy?.steps?.[stepIndex]?.params?.[key];
  }

  function renderParameterComparison(record, preview) {
    const originalStrategy = state.preview?.strategy || record.strategy;
    const previewStrategy = state.preview?.preview_strategy || record.strategy;
    const entries = parameterEntries(record);
    if (!entries.length) return "";
    const changed = new Set((state.changedSteps || []).map(Number));
    return `
      <div class="decision-param-compare">
        <div class="decision-compare-caption"><strong>参数对照</strong><span>${changed.size ? "参数已变化" : "参数未变化"}</span></div>
        ${entries.map(({ stepIndex, key, value }) => {
          const previewValue = strategyParamValue(previewStrategy, stepIndex, key);
          const differs = JSON.stringify(previewValue) !== JSON.stringify(strategyParamValue(originalStrategy, stepIndex, key));
          return `
            <div class="decision-param-compare-row ${differs ? "changed" : ""}">
              <span>${escapeHtml(PARAM_LABELS[key] || key)}</span>
              <b>原参数 ${escapeHtml(valueLabel(strategyParamValue(originalStrategy, stepIndex, key) ?? value))}</b>
              <b>临时参数 ${escapeHtml(valueLabel(previewValue ?? value))}</b>
            </div>
          `;
        }).join("")}
      </div>
    `;
  }

  function renderResultComparison(record) {
    const original = state.preview?.original || record.result || { status: "unknown", steps: [] };
    const preview = state.preview?.preview;
    if (!preview) {
      return `
        <div class="decision-compare-placeholder">
          <strong>尚未运行临时参数</strong>
          <p>上面的原始判断不会改变。修改现有参数后运行比较，预览结果会与原始结果并列展示。</p>
        </div>
      `;
    }
    const previewStrategy = state.preview?.preview_strategy || record.strategy;
    const changed = new Set((state.changedSteps || []).map(Number));
    const originalSteps = original.steps || [];
    const previewSteps = preview.steps || [];
    const length = Math.max(originalSteps.length, previewSteps.length);
    return `
      ${renderParameterComparison(record, preview)}
      <div class="decision-result-head">
        <div><span class="strategy-label">结果对照</span><h4>原参数与临时参数使用同一数据版本</h4></div>
        <span class="decision-compare-version">版本：${escapeHtml(selectedVersion()?.id || "original")}</span>
      </div>
      <div class="decision-result-columns">
        <section class="decision-result-column" aria-label="原参数结果">
          <div class="decision-result-column-head"><strong>原参数</strong>${statusBadge(original.status)}</div>
          ${renderStepList(originalSteps)}
        </section>
        <section class="decision-result-column" aria-label="临时参数结果">
          <div class="decision-result-column-head"><strong>临时参数</strong>${statusBadge(preview.status)}</div>
          ${renderSalePrice(previewSteps)}
          ${previewSteps.map((step, index) => changed.has(index) ? `
            <article class="decision-step decision-step--${escapeHtml(stepStatus(step))} decision-step--changed">
              <div class="decision-step-head">
                <div><span class="decision-step-index">${String(index + 1).padStart(2, "0")}</span><strong>${escapeHtml(stepName(step, index))}</strong></div>
                <span class="decision-changed-badge">已变化</span>
                ${statusBadge(step?.status)}
              </div>
              <p class="decision-step-reason">${escapeHtml(stepReason(step))}</p>
              ${(step?.actual != null || step?.threshold != null) ? `<div class="decision-step-metrics"><span><small>实际值</small><b>${escapeHtml(formatMetric(step.actual, "actual", step))}</b></span><span><small>阈值</small><b>${escapeHtml(formatMetric(step.threshold, "threshold", step))}</b></span></div>` : ""}
            </article>
          ` : renderPreviewStep(step, index)).join("")}
        </section>
      </div>
      <p class="decision-preview-caveat">预演不会提交订单、付款、上架、确认或调价任务。单项通过并不代表整个策略通过，也不代表真实交易已发生。</p>
    `;
  }

  function renderPreviewStep(step, index) {
    if (!step) return "";
    return `
      <article class="decision-step decision-step--${escapeHtml(stepStatus(step))}">
        <div class="decision-step-head">
          <div><span class="decision-step-index">${String(index + 1).padStart(2, "0")}</span><strong>${escapeHtml(stepName(step, index))}</strong></div>
          ${statusBadge(step?.status)}
        </div>
        <p class="decision-step-reason">${escapeHtml(stepReason(step))}</p>
        ${(step?.actual != null || step?.threshold != null) ? `<div class="decision-step-metrics"><span><small>实际值</small><b>${escapeHtml(formatMetric(step.actual, "actual", step))}</b></span><span><small>阈值</small><b>${escapeHtml(formatMetric(step.threshold, "threshold", step))}</b></span></div>` : ""}
      </article>
    `;
  }

  function invalidatePreviewForDraftChange() {
    state.simulationRequestToken += 1;
    state.preview = null;
    state.changedSteps = [];
    const button = byId("decision-simulate");
    if (button) {
      button.disabled = false;
      button.textContent = "运行参数比较";
    }
    const comparison = byId("decision-comparison");
    if (comparison && state.detail?.record) {
      comparison.innerHTML = renderResultComparison(state.detail.record);
    }
  }

  function historyValues(container) {
    if (Array.isArray(container)) return container;
    if (!container || typeof container !== "object") return [];
    if (Array.isArray(container.points)) return container.points;
    if (Array.isArray(container.rows)) return container.rows;
    if (Array.isArray(container.items)) return container.items;
    if (Array.isArray(container.series)) return container.series;
    if (container.points && typeof container.points === "object") {
      return Object.entries(container.points).map(([at, point]) => {
        if (Array.isArray(point)) return [point[0] || at, point[1], point[2]];
        if (point && typeof point === "object") return { at, ...point };
        return { at, price: point };
      });
    }
    if (Array.isArray(container.prices)) {
      const volumes = Array.isArray(container.volumes) ? container.volumes : [];
      return container.prices.map((price, index) => {
        if (price && typeof price === "object") {
          return { ...price, volume: price.volume ?? volumes[index] };
        }
        return { price, volume: volumes[index] };
      });
    }
    if (Array.isArray(container.price)) {
      const volumes = Array.isArray(container.volume || container.volumes) ? (container.volume || container.volumes) : [];
      return container.price.map((price, index) => ({ price, volume: volumes[index] }));
    }
    return [];
  }

  function firstFinite(object, keys) {
    for (const key of keys) {
      const value = object?.[key];
      if (value == null || value === "") continue;
      const number = Number(value);
      if (Number.isFinite(number)) return number;
    }
    return null;
  }

  function normalizeHistoryPoint(point) {
    if (point == null || point === "") return { missing: true };
    if (typeof point === "number") return { price: point, volume: null };
    if (Array.isArray(point)) {
      const price = firstFinite({ price: point[1] }, ["price"]);
      const volume = firstFinite({ volume: point[2] }, ["volume"]);
      const at = point[0] ?? "";
      return { price, volume, at, missing: price == null && volume == null };
    }
    if (typeof point !== "object") return { missing: true };
    const price = firstFinite(point, ["price", "value", "avg_price", "average_price", "median_price", "sale_price", "steam_price", "buff_price", "close"]);
    const volume = firstFinite(point, ["volume", "volumes", "sold", "count", "sales", "quantity"]);
    const at = point.at || point.time || point.timestamp || point.ts || point.date || point.captured_at || point.collected_at || "";
    return { price, volume, at, missing: price == null && volume == null };
  }

  function resolveHistory(inputs) {
    const kind = state.detail?.record?.kind;
    const candidates = kind === "sell"
      ? [inputs?.trend, inputs?.market?.history, inputs?.history, inputs?.price_history, inputs?.market_history, inputs?.history_data, inputs?.chart_history, inputs?.price_volume_history, inputs?.chart]
      : [inputs?.market?.history, inputs?.history, inputs?.price_history, inputs?.market_history, inputs?.history_data, inputs?.chart_history, inputs?.price_volume_history, inputs?.trend, inputs?.market, inputs?.chart];
    let unavailable = null;
    for (const container of candidates) {
      if (!container || typeof container !== "object") continue;
      const values = historyValues(container);
      const points = values.map(normalizeHistoryPoint);
      if (points.some((point) => point.price != null) || points.some((point) => point.volume != null)) {
        return { points, container, available: container.available };
      }
      if (container.available === false && !unavailable) unavailable = container;
    }
    return {
      points: [],
      container: unavailable || inputs,
      available: unavailable ? false : undefined,
    };
  }

  function chartNumberLabel(value) {
    if (value == null || !Number.isFinite(Number(value))) return "—";
    return Number(value).toLocaleString("zh-CN", { maximumFractionDigits: 2 });
  }

  function chartSvg(points) {
    const width = 680;
    const height = 300;
    const left = 54;
    const right = 18;
    const priceTop = 22;
    const priceBottom = 154;
    const volumeTop = 176;
    const volumeBottom = 250;
    const plotWidth = width - left - right;
    const count = Math.max(points.length, 1);
    const xAt = (index) => left + (count <= 1 ? plotWidth / 2 : (index / (count - 1)) * plotWidth);
    const prices = points.map((point) => point.price).filter((value) => Number.isFinite(Number(value))).map(Number);
    const volumes = points.map((point) => point.volume).filter((value) => Number.isFinite(Number(value))).map(Number);
    const minPrice = prices.length ? Math.min(...prices) : 0;
    const maxPrice = prices.length ? Math.max(...prices) : 1;
    const spread = maxPrice - minPrice || Math.max(1, Math.abs(maxPrice) * 0.1);
    const priceY = (value) => priceBottom - ((Number(value) - minPrice) / spread) * (priceBottom - priceTop);
    const maxVolume = volumes.length ? Math.max(...volumes) : 1;
    const bars = points.map((point, index) => {
      if (!Number.isFinite(Number(point.volume)) || point.volume == null) return "";
      const value = Number(point.volume);
      const barHeight = maxVolume > 0
        ? Math.max(1, (value / maxVolume) * (volumeBottom - volumeTop))
        : 0;
      const barWidth = Math.max(4, Math.min(18, plotWidth / Math.max(count, 1) * 0.55));
      return `<rect x="${(xAt(index) - barWidth / 2).toFixed(2)}" y="${(volumeBottom - barHeight).toFixed(2)}" width="${barWidth.toFixed(2)}" height="${barHeight.toFixed(2)}" rx="2" class="decision-chart-volume"></rect>`;
    }).join("");
    const segments = [];
    let segment = [];
    points.forEach((point, index) => {
      if (point.missing || point.price == null) {
        if (segment.length) segments.push(segment);
        segment = [];
        return;
      }
      segment.push(`${xAt(index).toFixed(2)},${priceY(point.price).toFixed(2)}`);
    });
    if (segment.length) segments.push(segment);
    const lines = segments.map((values) => values.length > 1
      ? `<polyline points="${values.join(" ")}" class="decision-chart-price-line"></polyline>`
      : values.length === 1
        ? `<circle cx="${values[0].split(",")[0]}" cy="${values[0].split(",")[1]}" r="3" class="decision-chart-price-dot"></circle>`
        : "").join("");
    const gaps = points.map((point, index) => point.missing || point.price == null
      ? `<line x1="${xAt(index).toFixed(2)}" x2="${xAt(index).toFixed(2)}" y1="${priceTop}" y2="${volumeBottom}" class="decision-chart-gap-line"></line>`
      : "").join("");
    const priceGuides = prices.length
      ? [0, 0.5, 1].map((ratio) => {
        const y = priceBottom - ratio * (priceBottom - priceTop);
        const value = minPrice + ratio * spread;
        return `<line x1="${left}" x2="${width - right}" y1="${y}" y2="${y}" class="decision-chart-guide"></line><text x="${left - 8}" y="${y + 4}" text-anchor="end" class="decision-chart-axis">${escapeHtml(chartNumberLabel(value))}</text>`;
      }).join("")
      : `<text x="${left}" y="${priceTop + 18}" class="decision-chart-axis">无价格数据</text>`;
    return `
      <svg class="decision-chart-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="价格与成交量走势图">
        <title>价格与成交量走势图</title>
        <text x="${left}" y="18" class="decision-chart-label">价格</text>
        <text x="${left}" y="${volumeTop - 9}" class="decision-chart-label">成交量</text>
        ${priceGuides}
        <line x1="${left}" x2="${width - right}" y1="${volumeBottom}" y2="${volumeBottom}" class="decision-chart-guide"></line>
        ${gaps}
        ${lines}
        ${bars}
        <text x="${width - right}" y="${height - 8}" text-anchor="end" class="decision-chart-axis">${points.length} 个时间点</text>
      </svg>
    `;
  }

  function renderChart(record) {
    const inputs = selectedInputs();
    const history = resolveHistory(inputs);
    const points = history.points;
    const meta = inputMetadata(inputs);
    const missingPriceCount = points.filter((point) => point.missing || point.price == null).length;
    const missingVolume = points.some((point) => point.price != null && point.volume == null);
    const hasVolume = points.some((point) => point.volume != null && Number.isFinite(Number(point.volume)));
    let note = "";
    if (!points.length) {
      if (history.available === false) {
        const reason = history.container?.reason || history.container?.error || "记录未提供原因";
        note = `历史行情不可用：${reason}；不会生成示例曲线。`;
      } else {
        note = "无可用历史行情；不会生成示例曲线。";
      }
    } else if (missingPriceCount) {
      note = `数据缺口：${missingPriceCount} 个时间点缺少价格，连线不会跨越缺口补造数据。`;
    }
    if (points.length && !hasVolume) {
      note += `${note ? " " : ""}记录中无可用成交量数据，未补造成交量。`;
    } else if (points.length && missingVolume) {
      note += `${note ? " " : ""}部分数据点缺少成交量，未补造成交量。`;
    }
    const version = selectedVersion();
    const versionId = version?.id || "original";
    return `
      <section class="decision-card decision-chart" id="decision-chart" data-version-id="${escapeHtml(versionId)}">
        <div class="decision-card-head">
          <div><span class="strategy-label">行情辅助</span><h4>价格与成交量走势</h4></div>
          <span class="decision-version-chip">${escapeHtml(versionId === "original" ? "原记录" : "新数据版本")}</span>
        </div>
        <div id="decision-chart-meta" class="decision-chart-meta">
          来源：${escapeHtml(meta.source)} · 采集：${escapeHtml(fullDate(meta.collectedAt))} · 窗口：${escapeHtml(meta.window)} · 币种：${escapeHtml(meta.currency)}
        </div>
        ${points.length ? chartSvg(points) : `<div class="decision-chart-empty">无可用历史行情；不会生成示例曲线。</div>`}
        ${note ? `<p class="decision-chart-note">${escapeHtml(note)}</p>` : ""}
      </section>
    `;
  }

  function renderRepriceExecution(record) {
    const suggestion = record.result?.suggestion || {};
    const execution = record.result?.execution || {};
    const states = {
      not_started: "未启动调价任务", planned: "任务已计划，尚未撤单",
      withdrawing: "正在核实撤单", waiting_inventory: "等待物品返回库存",
      ready: "物品已返库，等待重新上架", submitting: "正在核实上架请求",
      confirming: "等待确认或核实新挂单", completed: "记录时已完成调价",
      cancelled: "调价任务已取消", needs_review: "调价任务待核实",
    };
    const currency = typeof record.inputs?.currency === "string" ? record.inputs.currency : "币种未标注";
    const target = Number(suggestion.target_buyer_minor);
    const price = suggestion.target_buyer_minor != null && Number.isFinite(target)
      ? `，建议买家价 ${(target / 100).toFixed(2)} ${currency}` : "";
    const evidence = (value) => value === true ? "已有记录" : value === false ? "未发生" : "未记录";
    return `<section class="decision-card" id="decision-reprice-execution">
      <div class="decision-card-head"><div><span class="strategy-label">检查当时的状态</span><h4>调价建议与实际执行</h4></div></div>
      <p>${escapeHtml(suggestion.action === "adjust" ? `建议调价${price}` : "本次未建议执行调价")}</p>
      <p>${escapeHtml(states[execution.status] || "执行状态未记录")}</p>
      <p>撤单：${evidence(execution.withdrawn)} · 返库：${evidence(execution.returned)} · 重新上架：${evidence(execution.relisted)}</p>
      <p class="decision-version-note">以上为记录时的执行证据；建议价不代表已撤单或已完成重新上架。</p>
    </section>`;
  }

  function renderDetail() {
    const container = byId("decision-detail");
    const record = state.detail?.record;
    if (!container || !record) return;
    const readonly = record.kind === "reprice";
    const refresh = state.detail?.refresh;
    const refreshKey = normalizeStatus(refresh?.status || "");
    const refreshText = refreshKey === "succeeded"
      ? "已完成"
      : refreshKey === "running"
        ? "正在获取完整行情…"
        : refreshKey === "queued"
          ? "刷新任务等待中…"
          : refreshKey === "failed"
            ? `刷新失败：${refresh.error || "未获得可用数据"}`
            : "尚未刷新";
    container.innerHTML = `
      <div class="decision-record-head">
        <div class="decision-title-block">
          <span class="strategy-label">${escapeHtml(kindLabel(record.kind))} · ${escapeHtml(formatDate(record.evaluated_at))}</span>
          <h3 id="decision-detail-title">${escapeHtml(record?.item?.name || "未知商品")}</h3>
          <p>${escapeHtml(record?.item?.market_hash_name || "")}</p>
          <div class="decision-record-meta">
            <span>策略：${escapeHtml(record?.strategy?.name || record?.strategy?.id || "未知")}</span>
            <span>检查时间：${escapeHtml(fullDate(record?.evaluated_at))}</span>
          </div>
        </div>
        <div class="decision-title-status">
          <span class="strategy-label">原始结果</span>
          <span id="decision-detail-status">${statusBadge(record?.result?.status)}</span>
        </div>
      </div>
      ${readonly ? `<div class="decision-readonly-note" id="decision-readonly"><strong>自动调价记录只读</strong><span>这里只解释未到时间、价差不足、底线限制或数据不可用；首版不支持试改自动调价专属参数。</span></div>` : ""}
      ${readonly ? renderRepriceExecution(record) : ""}
      ${state.copyNotice ? `<div class="decision-copy-notice">${escapeHtml(state.copyNotice)}</div>` : ""}
      ${state.detailLoading ? `<div class="decision-detail-loading">正在读取检查记录…</div>` : ""}
      <div class="decision-detail-grid">
        <section class="decision-card decision-original-panel" id="decision-original-panel">
          <div class="decision-card-head">
            <div><span class="strategy-label">原始解释</span><h4>本次真实检查</h4></div>
            ${statusBadge(record?.result?.status)}
          </div>
          <div id="decision-steps">${renderStepList(record?.result?.steps || [])}</div>
        </section>
        ${renderDataCard()}
      </div>
      ${renderVersionControls(record)}
      ${renderChart(record)}
      ${readonly ? "" : `
        <section class="decision-card decision-parameter-card">
          <div class="decision-card-head">
            <div><span class="strategy-label">临时参数</span><h4>只修改原策略中已有的参数</h4></div>
            <span class="decision-version-chip">固定数据</span>
          </div>
          ${renderParamEditor(record)}
          <div class="decision-actions">
            <button type="button" class="btn btn-primary" id="decision-simulate">运行参数比较</button>
            <button type="button" class="btn btn-secondary" id="decision-copy">另存为未启用副本</button>
            <span>修改只存在于当前页面；保存副本不会启用策略。</span>
          </div>
        </section>
        <section class="decision-card decision-comparison" id="decision-comparison">
          ${renderResultComparison(record)}
        </section>
      `}
    `;
    renderVersionNote();
    bindDetailControls(record);
  }

  function bindDetailControls(record) {
    byId("decision-version")?.addEventListener("change", async (event) => {
      captureDraftParams();
      state.selectedVersionId = event.target.value;
      state.preview = null;
      state.changedSteps = [];
      renderDetail();
      if (record.kind !== "reprice") await runSimulation(state.draftParams);
    });
    byId("decision-refresh")?.addEventListener("click", () => startRefresh());
    byId("decision-simulate")?.addEventListener("click", () => {
      const params = captureDraftParams();
      runSimulation(params);
    });
    byId("decision-copy")?.addEventListener("click", () => copyPreviewStrategy());
    document.querySelectorAll("[data-preview-param-key]").forEach((input) => {
      const updateBooleanLabel = () => {
        if (input.dataset.previewParamType !== "boolean") return;
        const label = input.closest(".decision-param")?.querySelector(".decision-param-control b");
        if (label) label.textContent = input.checked ? "启用" : "关闭";
      };
      input.addEventListener("input", () => {
        captureDraftParams();
        invalidatePreviewForDraftChange();
        updateBooleanLabel();
      });
      input.addEventListener("change", () => {
        captureDraftParams();
        invalidatePreviewForDraftChange();
        updateBooleanLabel();
      });
    });
  }

  function simulationBody(params) {
    const body = {
      record_id: state.selectedRecordId,
      params: params || {},
    };
    if (state.selectedVersionId && state.selectedVersionId !== "original") {
      body.version_id = state.selectedVersionId;
    }
    return body;
  }

  async function runSimulation(params) {
    const token = ++state.simulationRequestToken;
    const recordId = state.selectedRecordId;
    const versionId = state.selectedVersionId;
    const button = byId("decision-simulate");
    if (button) {
      button.disabled = true;
      button.textContent = "比较中…";
    }
    try {
      const body = simulationBody(params);
      const data = await fetchJson(`${API}/strategies/simulate`, {
        method: "POST",
        body: JSON.stringify(body),
      });
      if (token !== state.simulationRequestToken || recordId !== state.selectedRecordId || versionId !== state.selectedVersionId) return;
      if (!data.ok) throw new Error(data.error || "策略预演失败");
      state.preview = data;
      state.changedSteps = Array.isArray(data.changed_steps) ? data.changed_steps : [];
      renderDetail();
      toast("策略预演完成", "原始解释保留；未提交任何真实交易。");
    } catch (error) {
      if (token !== state.simulationRequestToken || recordId !== state.selectedRecordId || versionId !== state.selectedVersionId) return;
      if (button) {
        button.disabled = false;
        button.textContent = "运行参数比较";
      }
      toast("策略预演失败", error.message || "");
    }
  }

  async function startRefresh() {
    if (state.refreshBusy || !state.detail?.record || state.detail.record.kind === "reprice") return;
    const recordId = state.selectedRecordId;
    const token = ++state.refreshPollToken;
    captureDraftParams();
    state.refreshBusy = true;
    state.detail.refresh = { status: "queued", id: "", error: "" };
    renderDetail();
    try {
      const data = await fetchJson(`${API}/strategy-decisions/${encodeURIComponent(recordId)}/refresh`, {
        method: "POST",
      });
      if (token !== state.refreshPollToken || recordId !== state.selectedRecordId) return;
      if (!data.ok) throw new Error(data.error || "无法启动刷新");
      state.detail.refresh = data.refresh || { status: "queued" };
      renderDetail();
      pollRefresh(token, recordId);
    } catch (error) {
      if (token !== state.refreshPollToken || recordId !== state.selectedRecordId) return;
      state.refreshBusy = false;
      state.detail.refresh = { status: "failed", error: error.message || "无法启动刷新" };
      renderDetail();
      toast("刷新失败", error.message || "");
    }
  }

  async function pollRefresh(token, recordId) {
    for (let attempt = 0; attempt < 60; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 500));
      if (token !== state.refreshPollToken || recordId !== state.selectedRecordId) return;
      try {
        const data = await fetchJson(`${API}/strategy-decisions/${encodeURIComponent(recordId)}`);
        if (token !== state.refreshPollToken || recordId !== state.selectedRecordId) return;
        if (!data.ok) throw new Error(data.error || "无法读取刷新状态");
        if (!data.refresh) {
          state.refreshBusy = false;
          state.detail = {
            record: data.record || state.detail.record,
            versions: data.versions || [],
            refresh: {
              status: "failed",
              error: "刷新任务已丢失或无法读取，记录可能已清理；原始解释仍保留。",
            },
          };
          renderDetail();
          toast("刷新失败", "刷新任务已丢失或无法读取，原始解释仍保留。");
          return;
        }
        state.detail = {
          record: data.record || state.detail.record,
          versions: data.versions || [],
          refresh: data.refresh,
        };
        const status = normalizeStatus(data.refresh?.status);
        if (["queued", "running"].includes(status)) {
          renderDetail();
          continue;
        }
        state.refreshBusy = false;
        if (status === "succeeded" && data.refresh?.version_id) {
          const versionId = data.refresh?.version_id || data.versions?.[data.versions.length - 1]?.id;
          if (versionId) state.selectedVersionId = versionId;
          invalidatePreviewForDraftChange();
          renderDetail();
          await runSimulation(state.draftParams);
        } else {
          if (status === "succeeded") {
            state.detail.refresh = { ...data.refresh, status: "failed", error: "刷新成功但未返回可用的数据版本" };
          }
          renderDetail();
          toast("刷新失败", state.detail.refresh?.error || "未获得可用数据");
        }
        return;
      } catch (error) {
        if (token !== state.refreshPollToken || recordId !== state.selectedRecordId) return;
        state.refreshBusy = false;
        state.detail.refresh = { status: "failed", error: error.message || "刷新状态读取失败" };
        renderDetail();
        toast("刷新失败", error.message || "");
        return;
      }
    }
    if (token !== state.refreshPollToken || recordId !== state.selectedRecordId) return;
    state.refreshBusy = false;
    state.detail.refresh = { status: "failed", error: "刷新等待超时，请稍后重试" };
    renderDetail();
  }

  async function copyPreviewStrategy() {
    const recordId = state.selectedRecordId;
    const params = captureDraftParams();
    const defaultName = `${state.detail?.record?.strategy?.name || "策略"} 预演副本`;
    const name = await appPrompt("另存为未启用副本", defaultName, {
      message: "副本会保留原策略结构，只应用这里修改过的既有参数。保存后不会自动启用，也不会改变原策略。",
      label: "副本名称",
      confirmText: "保存副本",
      cancelText: "取消",
    });
    if (!name || !String(name).trim()) return;
    try {
      const data = await fetchJson(`${API}/strategy-decisions/${encodeURIComponent(recordId)}/copy`, {
        method: "POST",
        body: JSON.stringify({ name: String(name).trim(), params }),
      });
      if (!data.ok) throw new Error(data.error || "保存副本失败");
      if (recordId !== state.selectedRecordId) {
        toast("副本已保存", "尚未启用，原策略保持不变。");
        return;
      }
      state.copyNotice = `已保存「${data.strategy?.name || name}」，尚未启用。请到策略中心查看并决定是否启用。`;
      renderDetail();
      toast("副本已保存", "尚未启用，原策略保持不变。");
    } catch (error) {
      toast("保存副本失败", error.message || "");
    }
  }

  async function openRecord(recordId) {
    if (!recordId) return;
    const token = ++state.detailRequestToken;
    state.selectedRecordId = recordId;
    state.detailLoading = true;
    state.detail = null;
    state.selectedVersionId = "original";
    state.draftParams = {};
    state.preview = null;
    state.changedSteps = [];
    state.copyNotice = "";
    state.refreshBusy = false;
    state.refreshPollToken += 1;
    state.simulationRequestToken += 1;
    renderList();
    const container = byId("decision-detail");
    if (container) {
      container.classList.add("hidden");
      container.innerHTML = `<div class="decision-detail-loading">正在读取检查记录…</div>`;
    }
    try {
      const data = await fetchJson(`${API}/strategy-decisions/${encodeURIComponent(recordId)}`);
      if (token !== state.detailRequestToken) return;
      if (!data.ok) throw new Error(data.error || "检查记录不可用或已清理");
      state.detail = {
        record: data.record,
        versions: Array.isArray(data.versions) ? data.versions : [],
        refresh: data.refresh || null,
      };
      const refreshVersion = data.refresh?.version_id;
      if (refreshVersion && normalizeStatus(data.refresh.status) === "succeeded") {
        state.selectedVersionId = refreshVersion;
      }
      state.detailLoading = false;
      renderDetail();
      container?.classList.remove("hidden");
      const refreshKey = normalizeStatus(state.detail.refresh?.status || "");
      if (["queued", "running"].includes(refreshKey)) {
        const token = ++state.refreshPollToken;
        pollRefresh(token, recordId);
      } else if (state.selectedVersionId !== "original") {
        await runSimulation({});
      }
    } catch (error) {
      if (token !== state.detailRequestToken) return;
      state.detailLoading = false;
      if (container) {
        container.innerHTML = `
          <div class="decision-detail-error" role="alert">
            <strong>检查记录不可用</strong>
            <p>${escapeHtml(error.message || "记录可能已过期或已清理，不会回退到示例结果。")}</p>
          </div>
        `;
        container.classList.remove("hidden");
      }
    }
  }

  function bind() {
    if (state.initialized) return;
    state.initialized = true;
    const filters = byId("decision-filters");
    filters?.addEventListener("submit", (event) => {
      event.preventDefault();
      state.filters = {
        kind: byId("decision-filter-kind")?.value || "buy",
        name: byId("decision-filter-name")?.value || "",
        status: byId("decision-filter-status")?.value || "",
      };
      loadList(1);
    });
    byId("decision-page-prev")?.addEventListener("click", () => loadList(Math.max(1, state.page - 1)));
    byId("decision-page-next")?.addEventListener("click", () => loadList(state.page + 1));
    byId("decision-reload")?.addEventListener("click", () => loadList(state.page));
  }

  function open() {
    bind();
    if (!state.listLoaded && !state.listLoading) loadList(1);
    else renderList();
  }

  window.DecisionPreview = {
    open,
    reload: () => loadList(state.page),
    state,
  };

  document.addEventListener("DOMContentLoaded", bind);
})();
