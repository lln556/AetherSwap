import {canonicalOriginFromUrl, permissionPatternForOrigin} from "./common.js";

const originNode = document.querySelector("#current-origin");
const statusNode = document.querySelector("#status");
const authorizeButton = document.querySelector("#authorize");
const revokeButton = document.querySelector("#revoke");
const openBuffLink = document.querySelector("#open-buff");
let current = null;

function setStatus(message, kind = "info") {
  statusNode.textContent = typeof message === "string" ? message : "";
  statusNode.dataset.kind = kind;
}

function setButtons({busy = false, authorized = false, permissionGranted = false} = {}) {
  authorizeButton.disabled = busy || authorized || !current;
  revokeButton.disabled = busy || (!authorized && !permissionGranted) || !current;
}

async function loadState(announce = true) {
  try {
    const result = await chrome.runtime.sendMessage({
      type: "GET_ORIGIN_STATE",
      origin: current.origin,
    });
    if (!result?.ok) {
      setStatus(result?.error || "无法读取当前站点授权", "error");
      setButtons();
      return;
    }
    setButtons({
      authorized: result.authorized === true,
      permissionGranted: result.permissionGranted === true,
    });
    if (announce) {
      setStatus(result.authorized ? "已授权当前来源" : "尚未授权当前来源");
    }
  } catch {
    setStatus("无法读取当前站点授权，请重新打开扩展", "error");
    setButtons();
  }
}

async function initialize() {
  let tab;
  try {
    [tab] = await chrome.tabs.query({active: true, currentWindow: true});
  } catch {
    setStatus("无法读取当前标签页，请重新打开扩展", "error");
    return;
  }
  const origin = canonicalOriginFromUrl(tab?.url);
  if (!origin) {
    originNode.textContent = "当前页面不是受支持的 AetherSwap 网页";
    setStatus("请在 AetherSwap 的 HTTP(S) 页面中打开此扩展", "error");
    return;
  }
  current = {origin, tabId: tab.id};
  originNode.textContent = origin;
  await loadState();
}

authorizeButton.addEventListener("click", async () => {
  if (!current) return;
  setButtons({busy: true});
  setStatus("正在请求当前来源权限…");
  try {
    // Keep this call in the button's user-gesture path so Chrome shows the
    // optional host-permission prompt.
    const granted = await chrome.permissions.request({
      origins: [permissionPatternForOrigin(current.origin)],
    });
    if (!granted) {
      setStatus("未取得当前来源权限；可再次点击授权", "error");
      await loadState(false);
      return;
    }
    const result = await chrome.runtime.sendMessage({
      type: "AUTHORIZE_ORIGIN",
      origin: current.origin,
      tabId: current.tabId,
    });
    if (!result?.ok) {
      setStatus(result?.error || "授权当前来源失败", "error");
      await loadState(false);
      return;
    }
    setStatus("已授权当前来源");
    setButtons({authorized: true});
  } catch {
    setStatus("授权未完成，请重新打开扩展后重试", "error");
    await loadState(false);
  }
});

revokeButton.addEventListener("click", async () => {
  if (!current) return;
  setButtons({busy: true});
  setStatus("正在撤销当前来源授权…");
  try {
    const result = await chrome.runtime.sendMessage({
      type: "REVOKE_ORIGIN",
      origin: current.origin,
    });
    if (!result?.ok) {
      setStatus(result?.error || "撤销授权失败", "error");
      await loadState(false);
      return;
    }
    setStatus(
      result.permissionRemoved === false
        ? "已撤销当前来源授权；Chrome 站点访问权限可在扩展详情中手动移除"
        : "已撤销当前来源授权",
    );
    setButtons({authorized: false});
  } catch {
    setStatus("撤销未完成，请重新打开扩展后重试", "error");
    await loadState(false);
  }
});

openBuffLink.addEventListener("click", () => {
  window.close();
});

void initialize();
