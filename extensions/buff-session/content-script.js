(() => {
  if (globalThis.__aetherswapBuffContentScript) return;
  globalThis.__aetherswapBuffContentScript = true;

  const PAGE_SOURCE = "aetherswap-buff-page";
  const EXTENSION_SOURCE = "aetherswap-buff-extension";
  const CONFIRMATION_TIMEOUT = 70_000;
  const CANCELLED_ERROR = "已取消等待确认，未读取或发送 BUFF 会话。";
  const UNCONFIRMED_ERROR =
    "导入请求已经发出，结果未确认，无法断言是否已保存。请刷新页面或 GET /api/auth/buff/session 查看最新状态。";
  const documentNonce = [...crypto.getRandomValues(new Uint8Array(16))]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
  let pendingImport = null;

  function validRequestId(value) {
    return typeof value === "string" && /^[A-Za-z0-9._:-]{1,128}$/.test(value);
  }

  function sanitizeSession(session) {
    if (!session || typeof session !== "object" || Array.isArray(session)) return null;
    const safe = {};
    for (const key of ["status", "message", "replace_block_reason"]) {
      if (typeof session[key] === "string") safe[key] = session[key].slice(0, 1000);
    }
    if (session.checked_at === null || Number.isFinite(session.checked_at)) {
      safe.checked_at = session.checked_at;
    }
    if (Number.isFinite(session.retry_after)) safe.retry_after = session.retry_after;
    if (typeof session.can_replace === "boolean") safe.can_replace = session.can_replace;
    return Object.keys(safe).length ? safe : null;
  }

  function post(type, requestId, result = {}) {
    const payload = {
      source: EXTENSION_SOURCE,
      type,
      requestId,
      ok: result.ok === true,
    };
    if (typeof result.error === "string" && result.error) payload.error = result.error.slice(0, 1000);
    if (typeof result.code === "string" && result.code) payload.code = result.code.slice(0, 1000);
    const session = sanitizeSession(result.session);
    if (session) payload.session = session;
    window.postMessage(payload, location.origin);
  }

  function finishPending(request, result) {
    if (!request || pendingImport !== request) return;
    const {requestId, timer} = request;
    clearTimeout(timer);
    pendingImport = null;
    post("RESULT", requestId, result);
  }

  async function cancelBackgroundPending(request) {
    try {
      await chrome.runtime.sendMessage({
        type: "CANCEL_PENDING_FROM_CONTENT",
        requestId: request.requestId,
        documentNonce: request.documentNonce,
      });
    } catch {
      // Clearing the local pending still ensures a later confirm click cannot
      // hand cookies to this page document.
    }
  }

  function safeRemoteResult(data) {
    if (!data || typeof data !== "object" || Array.isArray(data)) {
      return {ok: false, error: "AetherSwap 返回了无法识别的结果"};
    }
    const result = {ok: data.ok === true};
    if (typeof data.error === "string" && data.error) result.error = data.error.slice(0, 1000);
    if (typeof data.code === "string" && data.code) result.code = data.code.slice(0, 1000);
    const session = sanitizeSession(data.session);
    if (session) result.session = session;
    return result;
  }

  async function submitCookies(request, cookies) {
    try {
      const response = await fetch("/api/auth/buff/session/import", {
        method: "POST",
        mode: "same-origin",
        credentials: "same-origin",
        redirect: "error",
        cache: "no-store",
        headers: {
          "Content-Type": "application/json",
          "X-AetherSwap-Intent": "buff-session",
        },
        body: JSON.stringify({
          cookies,
          user_agent: navigator.userAgent,
          source: "extension",
          recovery: true,
        }),
      });
      let data;
      try {
        data = await response.json();
      } catch {
        finishPending(request, {ok: false, error: `AetherSwap 返回了无法解析的结果（HTTP ${response.status}）`});
        return;
      }
      if (!response.ok && (!data || typeof data !== "object")) {
        finishPending(request, {ok: false, error: `AetherSwap 请求失败（HTTP ${response.status}）`});
        return;
      }
      finishPending(request, safeRemoteResult(data));
    } catch {
      finishPending(request, {
        ok: false,
        error: "无法连接 AetherSwap，或请求被重定向。请确认服务已启动并保持当前页面登录。",
      });
    }
  }

  async function handlePageMessage(data) {
    if (!data || data.source !== PAGE_SOURCE || !validRequestId(data.requestId)) return;
    if (data.type === "PING") {
      try {
        const result = await chrome.runtime.sendMessage({
          type: "PING_FROM_PAGE",
          origin: location.origin,
        });
        post("PONG", data.requestId, {
          ok: result?.ok === true,
          error: result?.ok === true ? "" : result?.error || "扩展未授权当前来源",
        });
      } catch {
        post("PONG", data.requestId, {ok: false, error: "扩展服务不可用，请重新加载页面"});
      }
      return;
    }
    if (data.type === "CANCEL") {
      if (!pendingImport || pendingImport.requestId !== data.requestId) return;
      const request = pendingImport;
      request.cancelRequested = true;
      if (request.phase === "submitting") {
        finishPending(request, {ok: false, code: "result_unconfirmed", error: UNCONFIRMED_ERROR});
        return;
      }
      await cancelBackgroundPending(request);
      if (pendingImport === request) {
        if (request.phase === "submitting") {
          finishPending(request, {ok: false, code: "result_unconfirmed", error: UNCONFIRMED_ERROR});
        } else {
          finishPending(request, {ok: false, code: "cancelled", error: CANCELLED_ERROR});
        }
      }
      return;
    }
    if (data.type !== "IMPORT") return;
    if (pendingImport) {
      post("RESULT", data.requestId, {ok: false, error: "已有一次导入正在等待确认"});
      return;
    }

    const request = {
      requestId: data.requestId,
      documentNonce,
      timer: null,
      phase: "confirming",
      cancelRequested: false,
    };
    pendingImport = request;
    try {
      const started = await chrome.runtime.sendMessage({
        type: "START_IMPORT",
        origin: location.origin,
        requestId: data.requestId,
        documentNonce,
      });
      if (pendingImport !== request && !request.cancelRequested) return;
      if (!started?.ok) {
        finishPending(request, {ok: false, error: started?.error || "无法开始 BUFF 会话导入"});
        return;
      }
      if (request.cancelRequested) {
        await cancelBackgroundPending(request);
        return;
      }
      request.timer = setTimeout(async () => {
        if (pendingImport !== request) return;
        request.cancelRequested = true;
        await cancelBackgroundPending(request);
        if (request.phase === "submitting") {
          finishPending(request, {ok: false, code: "result_unconfirmed", error: UNCONFIRMED_ERROR});
        } else {
          finishPending(request, {ok: false, code: "timeout", error: "导入确认超时，请重新发起"});
        }
      }, CONFIRMATION_TIMEOUT);
    } catch {
      finishPending(request, {ok: false, error: "扩展服务不可用，请重新加载页面"});
    }
  }

  window.addEventListener("message", (event) => {
    if (event.source !== window || event.origin !== location.origin) return;
    void handlePageMessage(event.data);
  });

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type === "VERIFY_DOCUMENT") {
      sendResponse({
        ok:
          !pendingImport?.cancelRequested &&
          pendingImport?.phase === "confirming" &&
          pendingImport?.requestId === message.requestId &&
          pendingImport?.documentNonce === message.documentNonce &&
          message.origin === location.origin,
      });
      return false;
    }
    const request = pendingImport;
    if (message?.type === "IMPORT_FAILED") {
      if (
        pendingImport?.requestId === message.requestId &&
        pendingImport?.documentNonce === message.documentNonce
      ) {
        finishPending(request, {ok: false, code: message.code, error: message.error});
      }
      sendResponse({ok: true});
      return false;
    }
    if (message?.type !== "BUFF_IMPORT_COOKIES") return false;
    if (
      !pendingImport ||
      pendingImport.cancelRequested ||
      pendingImport.phase !== "confirming" ||
      pendingImport.requestId !== message.requestId ||
      pendingImport.documentNonce !== message.documentNonce ||
      message.origin !== location.origin ||
      typeof message.cookies !== "string"
    ) {
      sendResponse({ok: false, code: "document_changed"});
      return false;
    }
    pendingImport.phase = "submitting";
    sendResponse({ok: true});
    void submitCookies(request, message.cookies);
    return false;
  });
})();
