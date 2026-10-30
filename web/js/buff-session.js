(() => {
  "use strict";

  const SESSION_URL = `${API}/auth/buff/session`;
  const CHECK_URL = `${SESSION_URL}/check`;
  const IMPORT_URL = `${SESSION_URL}/import`;
  const PAGE_MESSAGE_SOURCE = "aetherswap-buff-page";
  const EXTENSION_MESSAGE_SOURCE = "aetherswap-buff-extension";
  const EXTENSION_PING_TIMEOUT = 1400;
  const EXTENSION_RESULT_TIMEOUT = 70000;

  const STATUS = {
    unconfigured: { badge: "未配置", text: "尚未保存 BUFF 登录会话。", tone: "muted" },
    unchecked: { badge: "尚未检测", text: "会话已保存，但还没有最近检测结果。", tone: "muted" },
    valid: { badge: "最近检测有效", text: "最近一次检测时，BUFF 接受了保存的会话。", tone: "ok" },
    expired: { badge: "最近检测已过期", text: "最近一次检测显示登录已失效。", tone: "bad" },
    verification: { badge: "需要安全验证", text: "最近一次检测要求先在 BUFF 完成安全验证。", tone: "warn" },
    rate_limited: { badge: "请求受限", text: "最近一次检测被 BUFF 限流。", tone: "warn" },
    error: { badge: "检测异常", text: "最近一次检测未能完成；这不等于登录已失效。", tone: "bad" },
    account_mismatch: { badge: "账号不匹配", text: "候选会话绑定的 Steam 账号与当前账号不一致。", tone: "bad" },
  };

  function formatCheckedAt(value) {
    if (!value) return "尚未检测";
    const date = new Date(Number(value) * 1000);
    if (Number.isNaN(date.getTime())) return "时间未知";
    return date.toLocaleString("zh-CN", { hour12: false });
  }

  const BuffSession = {
    initialized: false,
    session: null,
    loadPromise: null,
    checkInFlight: false,
    manualInFlight: false,
    manualAbort: null,
    pendingExtension: null,
    extensionTimer: null,
    extensionResultTimeout: EXTENSION_RESULT_TIMEOUT,
    extensionCancelTimeout: 5000,
    actionNotice: null,
    stateVersion: 0,
    cacheRequestSeq: 0,

    init() {
      if (this.initialized) return this.loadPromise;
      this.initialized = true;
      this.bind();
      this.loadPromise = this.loadCachedSession();
      return this.loadPromise;
    },

    bind() {
      el("btn-buff-check")?.addEventListener("click", () => this.checkSession());
      el("btn-buff-import")?.addEventListener("click", () => this.startBrowserImport());
      el("btn-buff-cancel-import")?.addEventListener("click", () => this.cancelBrowserImport());
      el("btn-buff-manual")?.addEventListener("click", () => this.openManualDialog());
      el("btn-buff-extension-guide")?.addEventListener("click", () => this.openExtensionGuide());
      el("btn-buff-extension-close")?.addEventListener("click", () => this.closeExtensionGuide());
      el("btn-buff-extension-done")?.addEventListener("click", () => this.closeExtensionGuide());
      window.addEventListener("message", (event) => { void this.handleExtensionMessage(event); });
      el("btn-buff-manual-cancel")?.addEventListener("click", () => this.closeManualDialog(true));
      el("btn-buff-manual-close")?.addEventListener("click", () => this.closeManualDialog(true));
      el("buff-manual-form")?.addEventListener("submit", (event) => {
        event.preventDefault();
        this.submitManualImport();
      });
      el("buff-manual-dialog")?.addEventListener("cancel", (event) => {
        event.preventDefault();
        if (this.manualInFlight) {
          this.setManualError("导入请求处理中；为避免误报取消，当前不能关闭。请等待服务端结果。");
          return;
        }
        this.closeManualDialog(true);
      });
    },

    openManualDialog() {
      if (this.session && !this.session.can_replace) return;
      this.setManualError("");
      const dialog = el("buff-manual-dialog");
      if (!dialog) return;
      if (typeof dialog.showModal === "function") dialog.showModal();
      else dialog.setAttribute("open", "");
      requestAnimationFrame(() => el("buff-cookies-input")?.focus());
    },

    closeManualDialog(clear = false, { force = false } = {}) {
      if (this.manualInFlight && !force) {
        this.setManualError("导入请求处理中；为避免误报取消，当前不能关闭。请等待服务端结果。");
        return false;
      }
      if (this.manualAbort) {
        if (!force) {
          this.stateVersion += 1;
          this.manualAbort.abort();
        }
        this.manualAbort = null;
      }
      this.manualInFlight = false;
      const dialog = el("buff-manual-dialog");
      if (!dialog) return;
      if (typeof dialog.close === "function" && dialog.open) dialog.close();
      else dialog.removeAttribute("open");
      this.setManualBusy(false);
      if (clear) this.clearManualForm();
      return true;
    },

    clearManualForm() {
      const cookies = el("buff-cookies-input");
      const userAgent = el("buff-user-agent-input");
      if (cookies) cookies.value = "";
      if (userAgent) userAgent.value = "";
      this.setManualError("");
    },

    setManualError(message) {
      const node = el("buff-manual-error");
      if (!node) return;
      node.textContent = message || "";
      node.classList.toggle("hidden", !message);
    },

    setManualBusy(busy) {
      const submit = el("btn-buff-manual-submit");
      const cancel = el("btn-buff-manual-cancel");
      const close = el("btn-buff-manual-close");
      if (submit) {
        submit.disabled = busy;
        submit.textContent = busy ? "验证中…" : "验证并替换";
      }
      if (cancel) cancel.disabled = busy;
      if (close) close.disabled = busy;
    },

    async submitManualImport() {
      if (this.manualInFlight) return;
      const cookiesNode = el("buff-cookies-input");
      const userAgentNode = el("buff-user-agent-input");
      const cookies = cookiesNode?.value || "";
      const userAgentOverride = (userAgentNode?.value || "").trim();
      const userAgent = userAgentOverride || navigator.userAgent;
      if (!cookies.trim()) {
        this.setManualError("请粘贴 BUFF Cookie。");
        cookiesNode?.focus();
        return;
      }

      this.manualInFlight = true;
      this.stateVersion += 1;
      this.manualAbort = new AbortController();
      this.setManualError("");
      this.setManualBusy(true);
      try {
        const data = await fetchJson(IMPORT_URL, {
          method: "POST",
          headers: { "X-AetherSwap-Intent": "buff-session" },
          body: JSON.stringify({ cookies, user_agent: userAgent, source: "manual", recovery: true }),
          signal: this.manualAbort.signal,
        });
        this.stateVersion += 1;
        if (data.session) this.renderSession(data.session);
        if (data.ok) {
          this.setActionMessage("");
          this.closeManualDialog(true, { force: true });
          return;
        }
        this.setManualError(data.error || data.session?.message || "会话验证未通过，原会话未被替换。");
      } catch (error) {
        if (error?.name === "AbortError") return;
        const detail = error?.message ? `（${error.message}）` : "";
        this.setManualError(
          `请求结果未确认${detail}。请点击“检测登录”或刷新页面读取服务端状态；此时不能确认原会话是否已改变。`
        );
      } finally {
        this.manualInFlight = false;
        this.manualAbort = null;
        this.setManualBusy(false);
      }
    },

    newRequestId() {
      if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
      return `buff-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    },

    browserImportSupported() {
      const mobileUa = /Android|iPhone|iPad|iPod|Mobile|Windows Phone|IEMobile|Opera Mini/i;
      const isMobile = navigator.userAgentData?.mobile === true || mobileUa.test(navigator.userAgent || "");
      return !isMobile;
    },

    startBrowserImport() {
      if (this.pendingExtension) return;
      if (this.session && !this.session.can_replace) return;
      if (!this.browserImportSupported()) {
        this.setActionMessage("从浏览器导入仅支持桌面 Chrome；手机请使用手动填写。", "info");
        return;
      }
      this.stateVersion += 1;
      const requestId = this.newRequestId();
      this.pendingExtension = { requestId, phase: "ping" };
      this.hideExtensionHelp();
      this.setBrowserImportBusy(true, "正在查找扩展…");
      this.setActionMessage("正在查找浏览器扩展…", "info");
      window.postMessage({
        source: PAGE_MESSAGE_SOURCE,
        type: "PING",
        requestId,
      }, window.location.origin);
      this.extensionTimer = window.setTimeout(() => {
        if (this.pendingExtension?.requestId !== requestId) return;
        this.finishBrowserImport();
        this.showExtensionHelp("未检测到扩展，或扩展尚未授权当前服务。请下载并安装扩展，在扩展弹窗中授权当前服务，然后刷新页面。");
      }, EXTENSION_PING_TIMEOUT);
    },

    async handleExtensionMessage(event) {
      if (event.source !== window || event.origin !== window.location.origin) return;
      const data = event.data;
      if (!data || data.source !== EXTENSION_MESSAGE_SOURCE) return;
      const pending = this.pendingExtension;
      if (!pending || data.requestId !== pending.requestId) return;

      if (data.type === "PONG" && pending.phase === "ping") {
        window.clearTimeout(this.extensionTimer);
        if (data.ok === false) {
          this.finishBrowserImport();
          this.showExtensionHelp(data.error || "扩展尚未授权当前服务。请在扩展弹窗中授权当前服务，然后刷新页面。");
          return;
        }
        pending.phase = "import";
        this.setBrowserImportBusy(true, "等待扩展确认…");
        this.setActionMessage("等待扩展确认导入会话…", "info");
        window.postMessage({
          source: PAGE_MESSAGE_SOURCE,
          type: "IMPORT",
          requestId: pending.requestId,
        }, window.location.origin);
        this.extensionTimer = window.setTimeout(() => {
          if (this.pendingExtension?.requestId !== pending.requestId) return;
          this.requestExtensionCancel("timeout");
        }, this.extensionResultTimeout);
        return;
      }

      if (data.type === "RESULT" && pending.phase === "import") {
        window.clearTimeout(this.extensionTimer);
        this.stateVersion += 1;
        const resultSession = data.session && typeof data.session === "object" ? data.session : null;
        if (resultSession) this.renderSession(resultSession);
        const ok = data.ok === true;
        const errorText = data.error || resultSession?.message || "扩展未能完成会话导入。";
        this.finishBrowserImport();
        if (ok && resultSession) {
          this.setActionMessage(resultSession.message || STATUS[resultSession.status]?.text || "扩展已完成会话导入。", "success");
        } else {
          this.setActionMessage(errorText, "error");
        }
        this.syncReplacementButtons();
        const resultVersion = this.stateVersion;
        const refreshed = await this.refreshCachedSession();
        if (ok && refreshed) this.setActionMessage("");
        if (!ok && this.stateVersion === resultVersion) {
          this.setActionMessage(errorText, "error");
        }
        return;
      }

      if (data.type === "RESULT" && pending.phase === "cancelling") {
        window.clearTimeout(this.extensionTimer);
        this.stateVersion += 1;
        const resultSession = data.session && typeof data.session === "object" ? data.session : null;
        if (resultSession) this.renderSession(resultSession);
        if (data.code === "cancelled") {
          const message = data.error || "已取消等待确认，未读取或发送 BUFF 会话。";
          this.finishBrowserImport();
          this.setActionMessage(message, "success");
          return;
        }

        const unconfirmed = data.error
          || "导入请求已经发出，结果未确认。请检测登录或刷新页面读取服务端状态。";
        const ok = data.ok === true;
        this.finishBrowserImport();
        this.setActionMessage(unconfirmed, ok ? "success" : "error");
        await this.refreshCachedSession();
      }
    },

    cancelBrowserImport() {
      this.requestExtensionCancel("user");
    },

    requestExtensionCancel(reason) {
      const pending = this.pendingExtension;
      if (!pending || pending.phase === "cancelling") return;
      window.clearTimeout(this.extensionTimer);
      window.postMessage({
        source: PAGE_MESSAGE_SOURCE,
        type: "CANCEL",
        requestId: pending.requestId,
      }, window.location.origin);

      if (pending.phase === "ping") {
        this.finishBrowserImport();
        this.setActionMessage("已请求扩展取消；本次尚未发送会话导入请求。", "info");
        return;
      }

      pending.phase = "cancelling";
      pending.cancelReason = reason;
      this.setBrowserImportBusy(true, reason === "timeout" ? "导入超时，正在取消…" : "等待扩展取消…");
      const cancel = el("btn-buff-cancel-import");
      if (cancel) {
        cancel.disabled = true;
        cancel.classList.add("hidden");
      }
      const badge = el("buff-session-badge");
      if (badge) badge.textContent = reason === "timeout" ? "导入超时" : "正在取消";
      this.setActionMessage(
        reason === "timeout"
          ? "扩展未在时限内返回导入结果，已请求撤销待确认导入；若请求已经发出，结果可能未确认。"
          : "已请求扩展撤销待确认导入；正在等待扩展确认是否尚未发送。",
        "info"
      );
      this.extensionTimer = window.setTimeout(() => {
        if (this.pendingExtension !== pending || pending.phase !== "cancelling") return;
        this.finishBrowserImport();
        this.setActionMessage(
          "取消结果未确认；扩展可能已经发送导入请求。请检测登录或刷新页面读取服务端状态。",
          "error"
        );
        void this.refreshCachedSession();
      }, this.extensionCancelTimeout);
    },

    showExtensionHelp(message) {
      const box = el("buff-extension-help");
      const output = el("buff-extension-help-message");
      if (output && message) output.textContent = message;
      box?.classList.remove("hidden");
      this.setActionMessage(message || "浏览器扩展未就绪，请查看安装与授权说明。", "error");
    },

    hideExtensionHelp() {
      el("buff-extension-help")?.classList.add("hidden");
    },

    openExtensionGuide() {
      const dialog = el("buff-extension-dialog");
      if (!dialog) return;
      if (typeof dialog.showModal === "function") dialog.showModal();
      else dialog.setAttribute("open", "");
    },

    closeExtensionGuide() {
      const dialog = el("buff-extension-dialog");
      if (!dialog) return;
      if (typeof dialog.close === "function" && dialog.open) dialog.close();
      else dialog.removeAttribute("open");
    },

    setBrowserImportBusy(busy, label = "从浏览器导入") {
      const button = el("btn-buff-import");
      if (button) {
        button.disabled = busy;
        button.textContent = label;
      }
      const manual = el("btn-buff-manual");
      if (busy) manual && (manual.disabled = true);
      const cancel = el("btn-buff-cancel-import");
      if (cancel) {
        cancel.disabled = !busy;
        cancel.classList.toggle("hidden", !busy);
      }
      const badge = el("buff-session-badge");
      if (badge && busy) {
        badge.textContent = "正在导入";
        badge.dataset.status = "importing";
      }
    },

    finishBrowserImport() {
      this.stateVersion += 1;
      window.clearTimeout(this.extensionTimer);
      this.extensionTimer = null;
      this.pendingExtension = null;
      const button = el("btn-buff-import");
      if (button) {
        button.textContent = this.browserImportSupported() ? "从浏览器导入" : "浏览器导入仅桌面";
      }
      const cancel = el("btn-buff-cancel-import");
      if (cancel) {
        cancel.disabled = true;
        cancel.classList.add("hidden");
      }
      this.syncReplacementButtons();
      if (this.session) this.renderSession(this.session);
    },

    syncReplacementButtons() {
      const canReplace = !!this.session?.can_replace;
      const browserSupported = this.browserImportSupported();
      const importButton = el("btn-buff-import");
      const manualButton = el("btn-buff-manual");
      if (importButton && !this.pendingExtension) {
        importButton.disabled = !canReplace || !browserSupported;
        importButton.title = browserSupported ? "" : "从浏览器导入仅支持桌面 Chrome；手机请使用手动填写。";
      }
      if (manualButton) manualButton.disabled = !canReplace || !!this.pendingExtension;
      const reason = el("buff-replace-reason");
      if (reason && this.session) {
        reason.textContent = canReplace
          ? "当前未阻止替换；导入仍需服务端再次验证。"
          : this.session.replace_block_reason || "当前条件下不能替换 BUFF 会话。";
      }
    },

    async checkSession() {
      if (this.checkInFlight) return;
      this.checkInFlight = true;
      this.stateVersion += 1;
      this.setActionState("checking", "正在检测登录…");
      try {
        const data = await fetchJson(CHECK_URL, {
          method: "POST",
          headers: { "X-AetherSwap-Intent": "buff-session" },
          body: "{}",
        });
        this.stateVersion += 1;
        if (data.session) this.renderSession(data.session);
        if (data.ok) {
          this.setActionMessage("");
        } else {
          this.setActionMessage(data.error || data.session?.message || "本次检测未完成，请稍后重试。", "error");
        }
      } catch (error) {
        this.stateVersion += 1;
        this.setActionMessage(error?.message || "本次检测未能完成；这不等于登录已失效。", "error");
      } finally {
        this.checkInFlight = false;
        this.setActionState("ready");
      }
    },

    setActionState(state, message = "") {
      const check = el("btn-buff-check");
      const badge = el("buff-session-badge");
      if (check) {
        check.disabled = state === "checking";
        check.textContent = state === "checking" ? "检测中…" : "检测登录";
      }
      if (badge && state === "checking") {
        badge.textContent = "正在检测";
        badge.dataset.status = "checking";
      }
      if (message) this.setActionMessage(message, "info");
    },

    setActionMessage(message, kind = "info") {
      this.actionNotice = message ? { message, kind } : null;
      this.renderActionMessage();
    },

    renderActionMessage() {
      const output = el("buff-session-message");
      if (!output) return;
      if (this.actionNotice) {
        output.textContent = this.actionNotice.message;
        output.dataset.kind = this.actionNotice.kind;
        return;
      }
      const current = this.session || { status: "unchecked", checked_at: null, message: "" };
      const status = STATUS[current.status] || STATUS.error;
      output.textContent = current.message || status.text;
      output.dataset.kind = "status";
    },

    hasActiveInteraction() {
      return this.checkInFlight || this.manualInFlight || !!this.pendingExtension;
    },

    async loadCachedSession({ showError = true } = {}) {
      const version = this.stateVersion;
      const requestSeq = ++this.cacheRequestSeq;
      try {
        const data = await fetchJson(SESSION_URL);
        if (!data.ok || !data.session || typeof data.session !== "object") {
          throw new Error("无法读取登录状态");
        }
        if (version !== this.stateVersion || requestSeq !== this.cacheRequestSeq || this.hasActiveInteraction()) {
          return false;
        }
        this.renderSession(data.session || null);
        return true;
      } catch (error) {
        if (version !== this.stateVersion || requestSeq !== this.cacheRequestSeq || this.hasActiveInteraction()) {
          return false;
        }
        if (showError) {
          this.renderUnavailable("无法读取最近检测结果，请刷新页面重试。");
        }
        return false;
      }
    },

    refreshCachedSession(options = {}) {
      return this.loadCachedSession(options);
    },

    renderSession(session) {
      this.session = session && typeof session === "object" ? session : null;
      const current = this.session || { status: "unchecked", checked_at: null, message: "" };
      const status = STATUS[current.status] || STATUS.error;
      const badge = el("buff-session-badge");
      const checked = el("buff-session-checked-at");
      const retry = el("buff-session-retry");
      const reason = el("buff-replace-reason");
      if (badge) {
        badge.textContent = status.badge;
        badge.dataset.status = current.status || "error";
      }
      this.renderActionMessage();
      if (checked) checked.textContent = formatCheckedAt(current.checked_at);
      const retrySeconds = Number(current.retry_after);
      if (retry) {
        const showRetry = current.status === "rate_limited" && Number.isFinite(retrySeconds) && retrySeconds > 0;
        retry.classList.toggle("hidden", !showRetry);
        const value = retry.querySelector("dd");
        if (value && showRetry) value.textContent = `约 ${Math.ceil(retrySeconds)} 秒后可再次检测；请勿连续重试。`;
      }
      if (reason) {
        reason.textContent = current.can_replace
          ? "当前未阻止替换；导入仍需服务端再次验证。"
          : current.replace_block_reason || "当前条件下不能替换 BUFF 会话。";
      }
      const importButton = el("btn-buff-import");
      const manualButton = el("btn-buff-manual");
      if (importButton) {
        const browserSupported = this.browserImportSupported();
        importButton.disabled = !current.can_replace || !browserSupported;
        importButton.textContent = browserSupported ? "从浏览器导入" : "浏览器导入仅桌面";
        importButton.title = browserSupported ? "" : "从浏览器导入仅支持桌面 Chrome；手机请使用手动填写。";
      }
      if (manualButton) manualButton.disabled = !current.can_replace;
    },

    renderUnavailable(message) {
      const badge = el("buff-session-badge");
      const output = el("buff-session-message");
      const reason = el("buff-replace-reason");
      if (badge) {
        badge.textContent = "状态暂不可用";
        badge.dataset.status = "error";
      }
      if (output && !this.actionNotice) output.textContent = message;
      if (reason) reason.textContent = "替换保护状态未知，已禁用导入入口；检测仍可单独使用。";
      const importButton = el("btn-buff-import");
      const manualButton = el("btn-buff-manual");
      if (importButton) importButton.disabled = true;
      if (manualButton) manualButton.disabled = true;
    },
  };

  window.BuffSession = BuffSession;
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => BuffSession.init(), { once: true });
  } else {
    BuffSession.init();
  }
})();
