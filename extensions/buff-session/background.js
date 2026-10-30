import {
  AUTHORIZED_ORIGINS_KEY,
  BUFF_ORIGIN,
  canonicalOriginFromUrl,
  isValidRequestId,
  permissionPatternForOrigin,
  safeText,
} from "./common.js";

const PENDING_PREFIX = "pendingImport:";
const CONFIRMATION_WIDTH = 480;
const CONFIRMATION_HEIGHT = 560;

// One worker owns all origin mutations. Serialize the entire storage/script/
// permission transaction, including different ports sharing a host pattern.
let originMutation = Promise.resolve();
function changeOrigins(operation) {
  const result = originMutation.then(operation);
  originMutation = result.catch(() => {});
  return result;
}

function extensionPage(sender) {
  const prefix = `chrome-extension://${chrome.runtime.id}/`;
  return typeof sender?.url === "string" && sender.url.startsWith(prefix);
}

async function normalWindow(windowId) {
  try {
    const window = await chrome.windows.get(windowId);
    return window?.type === "normal";
  } catch {
    return false;
  }
}

async function authorizedOrigins() {
  const stored = await chrome.storage.local.get(AUTHORIZED_ORIGINS_KEY);
  const origins = Array.isArray(stored[AUTHORIZED_ORIGINS_KEY])
    ? stored[AUTHORIZED_ORIGINS_KEY]
    : [];
  return [...new Set(origins.map(canonicalOriginFromUrl).filter(Boolean))];
}

async function permissionGranted(origin) {
  try {
    return await chrome.permissions.contains({
      origins: [permissionPatternForOrigin(origin)],
    });
  } catch {
    return false;
  }
}

async function exactOriginAuthorized(origin) {
  const canonical = canonicalOriginFromUrl(origin);
  if (!canonical) return false;
  const origins = await authorizedOrigins();
  return origins.includes(canonical) && (await permissionGranted(canonical));
}

async function targetTab(tabId, origin) {
  if (!Number.isInteger(tabId)) return null;
  let tab;
  try {
    tab = await chrome.tabs.get(tabId);
  } catch {
    return null;
  }
  if (!tab || tab.incognito || canonicalOriginFromUrl(tab.url) !== origin) return null;
  if (!(await normalWindow(tab.windowId))) return null;
  return tab;
}

async function trustedContentSender(sender) {
  if (sender?.id !== chrome.runtime.id || sender.frameId !== 0 || !sender.tab) return null;
  if (sender.tab.incognito || !(await normalWindow(sender.tab.windowId))) return null;
  const tabOrigin = canonicalOriginFromUrl(sender.tab.url);
  const documentOrigin = canonicalOriginFromUrl(sender.url);
  if (!tabOrigin || tabOrigin !== documentOrigin) return null;
  const current = await targetTab(sender.tab.id, tabOrigin);
  if (!current) return null;
  return {origin: tabOrigin, tab: current};
}

async function scriptIdForPattern(pattern) {
  const bytes = new TextEncoder().encode(pattern);
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
  return `aetherswap-${[...digest.slice(0, 12)].map((byte) => byte.toString(16).padStart(2, "0")).join("")}`;
}

async function registerContentScript(origin) {
  const pattern = permissionPatternForOrigin(origin);
  const id = await scriptIdForPattern(pattern);
  const existing = await chrome.scripting.getRegisteredContentScripts({ids: [id]});
  if (!existing.length) {
    await chrome.scripting.registerContentScripts([
      {
        id,
        matches: [pattern],
        js: ["content-script.js"],
        runAt: "document_start",
        allFrames: false,
        persistAcrossSessions: true,
      },
    ]);
  }
  return id;
}

async function injectCurrentDocument(tabId) {
  try {
    await chrome.scripting.executeScript({
      target: {tabId, frameIds: [0]},
      files: ["content-script.js"],
    });
    return true;
  } catch {
    // Registration will inject into the next permitted document.
    return false;
  }
}

async function authorizeOrigin(message, sender) {
  if (!extensionPage(sender)) return {ok: false, error: "仅扩展界面可以授权来源"};
  const origin = canonicalOriginFromUrl(message.origin);
  if (!origin) return {ok: false, error: "无效的服务来源"};
  const tab = await targetTab(message.tabId, origin);
  if (!tab) return {ok: false, error: "目标标签页已变化，请重新打开扩展"};
  if (!(await permissionGranted(origin))) {
    return {ok: false, error: "未取得当前 AetherSwap 来源的访问权限"};
  }

  await registerContentScript(origin);
  const origins = await authorizedOrigins();
  if (!origins.includes(origin)) {
    origins.push(origin);
    await chrome.storage.local.set({[AUTHORIZED_ORIGINS_KEY]: origins});
  }
  await injectCurrentDocument(tab.id);
  return {ok: true, origin};
}

async function revokeOrigin(message, sender) {
  if (!extensionPage(sender)) return {ok: false, error: "仅扩展界面可以撤销授权"};
  const origin = canonicalOriginFromUrl(message.origin);
  if (!origin) return {ok: false, error: "无效的服务来源"};
  const pattern = permissionPatternForOrigin(origin);
  const origins = await authorizedOrigins();
  const remaining = origins.filter((item) => item !== origin);
  await chrome.storage.local.set({[AUTHORIZED_ORIGINS_KEY]: remaining});

  const patternStillUsed = remaining.some(
    (item) => permissionPatternForOrigin(item) === pattern,
  );
  let permissionRemoved = true;
  if (!patternStillUsed) {
    try {
      await chrome.scripting.unregisterContentScripts({
        ids: [await scriptIdForPattern(pattern)],
      });
    } catch {
      // The registration may already have disappeared with its permission.
    }
    try {
      await chrome.permissions.remove({origins: [pattern]});
    } catch {
      permissionRemoved = false;
    }
  }
  return {ok: true, permissionRemoved};
}

async function originState(message, sender) {
  if (!extensionPage(sender)) return {ok: false, error: "仅扩展界面可以查询授权"};
  const origin = canonicalOriginFromUrl(message.origin);
  if (!origin) return {ok: false, error: "无效的服务来源"};
  return {
    ok: true,
    authorized: await exactOriginAuthorized(origin),
    permissionGranted: await permissionGranted(origin),
  };
}

async function pagePing(message, sender) {
  const trusted = await trustedContentSender(sender);
  if (!trusted || trusted.origin !== canonicalOriginFromUrl(message.origin)) {
    return {ok: false, error: "页面来源与标签页不一致"};
  }
  if (!(await exactOriginAuthorized(trusted.origin))) {
    return {ok: false, error: "当前 AetherSwap 来源尚未在扩展中授权"};
  }
  return {ok: true};
}

async function pendingRecords() {
  const all = await chrome.storage.session.get(null);
  return Object.entries(all)
    .filter(([key, value]) => key.startsWith(PENDING_PREFIX) && value && typeof value === "object")
    .map(([, value]) => value);
}

async function getPending(confirmationId) {
  if (typeof confirmationId !== "string" || !/^[a-f0-9-]{16,64}$/i.test(confirmationId)) {
    return null;
  }
  const key = PENDING_PREFIX + confirmationId;
  const stored = await chrome.storage.session.get(key);
  return stored[key] || null;
}

async function putPending(pending) {
  await chrome.storage.session.set({[PENDING_PREFIX + pending.confirmationId]: pending});
}

async function deletePending(confirmationId) {
  await chrome.storage.session.remove(PENDING_PREFIX + confirmationId);
}

async function pendingForTab(tabId) {
  return (await pendingRecords()).find((pending) => pending.tabId === tabId) || null;
}

async function closeConfirmation(pending) {
  if (!Number.isInteger(pending?.confirmationWindowId)) return;
  try {
    await chrome.windows.remove(pending.confirmationWindowId);
  } catch {
    // The user may already have closed the confirmation window.
  }
}

async function notifyPageFailure(pending, code, error) {
  try {
    await chrome.tabs.sendMessage(
      pending.tabId,
      {
        type: "IMPORT_FAILED",
        requestId: pending.requestId,
        documentNonce: pending.documentNonce,
        code,
        error,
      },
      {frameId: 0},
    );
  } catch {
    // Navigation may have destroyed the originating content script.
  }
}

async function failPending(pending, code, error, {close = true, notify = true} = {}) {
  if (!pending) return;
  await deletePending(pending.confirmationId);
  if (notify) await notifyPageFailure(pending, code, error);
  if (close) await closeConfirmation(pending);
}

async function confirmationStillActive(pending) {
  const current = await getPending(pending.confirmationId);
  return (
    current?.requestId === pending.requestId &&
    current?.documentNonce === pending.documentNonce
  );
}

async function startImport(message, sender) {
  const trusted = await trustedContentSender(sender);
  if (!trusted) return {ok: false, error: "页面来源与标签页不一致"};
  const origin = canonicalOriginFromUrl(message.origin);
  if (!origin || origin !== trusted.origin) {
    return {ok: false, error: "目标来源已变化，请重试"};
  }
  if (!isValidRequestId(message.requestId)) return {ok: false, error: "请求标识无效"};
  if (typeof message.documentNonce !== "string" || message.documentNonce.length > 128) {
    return {ok: false, error: "页面文档标识无效"};
  }
  if (!(await exactOriginAuthorized(origin))) {
    return {ok: false, error: "当前 AetherSwap 来源尚未在扩展中授权"};
  }
  if (await pendingForTab(trusted.tab.id)) {
    return {ok: false, error: "已有一次导入确认正在等待处理"};
  }

  const confirmationId = crypto.randomUUID();
  const pending = {
    confirmationId,
    requestId: message.requestId,
    documentNonce: message.documentNonce,
    origin,
    tabId: trusted.tab.id,
    windowId: trusted.tab.windowId,
    createdAt: Date.now(),
    confirmationWindowId: null,
  };
  await putPending(pending);
  try {
    const created = await chrome.windows.create({
      url: chrome.runtime.getURL(`confirm.html?id=${encodeURIComponent(confirmationId)}`),
      type: "popup",
      width: CONFIRMATION_WIDTH,
      height: CONFIRMATION_HEIGHT,
      focused: true,
    });
    pending.confirmationWindowId = created.id;
    await putPending(pending);
  } catch {
    await failPending(pending, "confirmation_failed", "无法打开导入确认窗口", {close: false});
    return {ok: false, error: "无法打开导入确认窗口"};
  }
  return {ok: true};
}

async function getConfirmation(message, sender) {
  if (!extensionPage(sender)) return {ok: false, error: "无效的确认页面"};
  const pending = await getPending(message.confirmationId);
  if (!pending) return {ok: false, code: "confirmation_expired", error: "确认已失效，请重新导入"};
  if (sender.tab && pending.confirmationWindowId !== sender.tab.windowId) {
    return {ok: false, code: "confirmation_expired", error: "确认窗口不匹配"};
  }
  return {ok: true, origin: pending.origin, createdAt: pending.createdAt};
}

function browserCookieHeader(cookies) {
  return (cookies || [])
    .filter((cookie) => cookie && typeof cookie.name === "string" && cookie.name)
    .map((cookie) => `${cookie.name}=${String(cookie.value ?? "")}`)
    .join("; ");
}

async function verifyDocument(pending) {
  try {
    const result = await chrome.tabs.sendMessage(
      pending.tabId,
      {
        type: "VERIFY_DOCUMENT",
        requestId: pending.requestId,
        documentNonce: pending.documentNonce,
        origin: pending.origin,
      },
      {frameId: 0},
    );
    return result?.ok === true;
  } catch {
    return false;
  }
}

async function confirmImport(message, sender) {
  if (!extensionPage(sender)) return {ok: false, error: "无效的确认页面"};
  const pending = await getPending(message.confirmationId);
  if (!pending) return {ok: false, code: "confirmation_expired", error: "确认已失效，请重新导入"};
  if (sender.tab && pending.confirmationWindowId !== sender.tab.windowId) {
    return {ok: false, code: "confirmation_expired", error: "确认窗口不匹配"};
  }
  if (pending.confirming) return {ok: false, code: "confirmation_busy", error: "正在处理本次导入"};

  pending.confirming = true;
  await putPending(pending);
  try {
    const tab = await targetTab(pending.tabId, pending.origin);
    if (!tab || !(await exactOriginAuthorized(pending.origin))) {
      await failPending(pending, "target_changed", "目标 AetherSwap 标签页或授权已变化，已取消导入");
      return {ok: false, code: "target_changed", error: "目标页面或授权已变化，已取消导入"};
    }
    if (!(await verifyDocument(pending))) {
      await failPending(pending, "document_changed", "目标页面已导航或刷新，已取消导入");
      return {ok: false, code: "document_changed", error: "目标页面已导航或刷新，已取消导入"};
    }
    if (!(await confirmationStillActive(pending))) {
      return {ok: false, code: "cancelled", error: "已取消等待确认，未读取或发送 BUFF 会话。"};
    }

    const cookies = await chrome.cookies.getAll({url: BUFF_ORIGIN});
    if (!cookies.some((cookie) => cookie.name === "session" && cookie.value)) {
      const error = "未检测到 BUFF 登录。请先在 Chrome 打开 https://buff.163.com/ 完成登录，再重新导入。";
      await failPending(pending, "buff_login_required", error);
      return {ok: false, code: "buff_login_required", error};
    }
    const cookieHeader = browserCookieHeader(cookies);

    const latestTab = await targetTab(pending.tabId, pending.origin);
    if (!latestTab || !(await exactOriginAuthorized(pending.origin))) {
      await failPending(pending, "target_changed", "目标页面或授权已变化，已取消导入");
      return {ok: false, code: "target_changed", error: "目标页面或授权已变化，已取消导入"};
    }
    if (!(await confirmationStillActive(pending))) {
      return {ok: false, code: "cancelled", error: "已取消等待确认，未发送 BUFF 会话。"};
    }
    let accepted;
    try {
      accepted = await chrome.tabs.sendMessage(
        pending.tabId,
        {
          type: "BUFF_IMPORT_COOKIES",
          requestId: pending.requestId,
          documentNonce: pending.documentNonce,
          origin: pending.origin,
          cookies: cookieHeader,
        },
        {frameId: 0},
      );
    } catch {
      accepted = null;
    }
    if (!accepted?.ok) {
      await failPending(pending, "document_changed", "目标页面已导航或刷新，已取消导入");
      return {ok: false, code: "document_changed", error: "目标页面已导航或刷新，已取消导入"};
    }

    await deletePending(pending.confirmationId);
    await closeConfirmation(pending);
    return {ok: true};
  } finally {
    const stillPending = await getPending(pending.confirmationId);
    if (stillPending?.confirming && stillPending !== pending) {
      stillPending.confirming = false;
      await putPending(stillPending);
    }
  }
}

async function cancelConfirmation(message, sender) {
  if (!extensionPage(sender)) return {ok: false, error: "无效的确认页面"};
  const pending = await getPending(message.confirmationId);
  if (!pending) return {ok: false, code: "confirmation_expired", error: "确认已失效"};
  if (sender.tab && pending.confirmationWindowId !== sender.tab.windowId) {
    return {ok: false, error: "确认窗口不匹配"};
  }
  await failPending(pending, "cancelled", "已取消 BUFF 会话导入");
  return {ok: true};
}

async function cancelFromContent(message, sender) {
  const trusted = await trustedContentSender(sender);
  if (
    !trusted ||
    !isValidRequestId(message.requestId) ||
    typeof message.documentNonce !== "string"
  ) {
    return {ok: false};
  }
  const pending = (await pendingRecords()).find(
    (item) =>
      item.tabId === trusted.tab.id &&
      item.requestId === message.requestId &&
      item.documentNonce === message.documentNonce,
  );
  if (pending) {
    await failPending(
      pending,
      "cancelled",
      "已取消等待确认，未读取或发送 BUFF 会话。",
      {notify: false},
    );
  }
  return {ok: true};
}

async function handleMessage(message, sender) {
  if (!message || typeof message !== "object") return {ok: false, error: "无效的消息"};
  switch (message.type) {
    case "AUTHORIZE_ORIGIN":
      return changeOrigins(() => authorizeOrigin(message, sender));
    case "REVOKE_ORIGIN":
      return changeOrigins(() => revokeOrigin(message, sender));
    case "GET_ORIGIN_STATE":
      return originState(message, sender);
    case "PING_FROM_PAGE":
      return pagePing(message, sender);
    case "START_IMPORT":
      return startImport(message, sender);
    case "GET_CONFIRMATION":
      return getConfirmation(message, sender);
    case "CONFIRM_IMPORT":
      return confirmImport(message, sender);
    case "CANCEL_CONFIRMATION":
      return cancelConfirmation(message, sender);
    case "CANCEL_PENDING_FROM_CONTENT":
      return cancelFromContent(message, sender);
    default:
      return {ok: false, error: "未知的扩展消息"};
  }
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  handleMessage(message, sender)
    .then(sendResponse)
    .catch((error) => sendResponse({ok: false, error: safeText(error?.message, "扩展操作失败")}));
  return true;
});

chrome.windows.onRemoved.addListener((windowId) => {
  void (async () => {
    const pending = (await pendingRecords()).find(
      (item) => item.confirmationWindowId === windowId,
    );
    if (pending) await failPending(pending, "cancelled", "已关闭导入确认窗口", {close: false});
  })();
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (!changeInfo.url && changeInfo.status !== "loading") return;
  void (async () => {
    const pending = await pendingForTab(tabId);
    if (pending) {
      await failPending(pending, "target_navigation_changed", "AetherSwap 页面已导航，已取消导入");
    }
  })();
});

chrome.tabs.onRemoved.addListener((tabId) => {
  void (async () => {
    const pending = await pendingForTab(tabId);
    if (pending) await failPending(pending, "target_closed", "AetherSwap 标签页已关闭，已取消导入");
  })();
});
