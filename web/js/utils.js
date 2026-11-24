const API = '/api';
const REFRESH_INTERVAL_DEFAULT = 60;
const REDACTED_SECRET_SENTINEL = "••••••••";
window.RUNTIME_PROFILE = null;

function el(id) {
  return document.getElementById(id);
}

function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function toast(title, detail = "", ttl = 3500, variant = "info") {
  const host = el("toast-host");
  if (!host) return;
  if (typeof host.showPopover === "function" && host.hasAttribute("popover")) {
    try {
      if (!host.matches(":popover-open")) host.showPopover();
    } catch {
    }
  }
  const node = document.createElement("div");
  const safeVariant = ["info", "success", "error", "warn"].includes(variant) ? variant : "info";
  node.className = `toast toast--${safeVariant}`;
  node.style.setProperty("--toast-ttl", `${ttl}ms`);
  node.innerHTML = `<div class="t">${escapeHtml(title)}</div>${detail ? `<div class="d">${escapeHtml(compactErrorText(detail, 220))}</div>` : ""}`;
  host.appendChild(node);
  setTimeout(() => {
    node.classList.add("toast-exit");
  }, Math.max(ttl - 400, 100));
  setTimeout(() => {
    node.remove();
    if (!host.children.length && typeof host.hidePopover === "function" && host.hasAttribute("popover")) {
      try {
        if (host.matches(":popover-open")) host.hidePopover();
      } catch {
      }
    }
  }, ttl);
}

function _isClickOutsideDialog(e, dialog) {
  const r = dialog.getBoundingClientRect();
  return e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom;
}

function bindDialogLightDismiss(dialog, onDismiss = null) {
  if (!dialog || dialog._lightDismissBound) return;
  dialog._lightDismissBound = true;
  if ("closedBy" in HTMLDialogElement.prototype) return;
  let pressedOutside = false;
  dialog.addEventListener("pointerdown", (e) => {
    pressedOutside = _isClickOutsideDialog(e, dialog);
  });
  dialog.addEventListener("pointerup", (e) => {
    if (pressedOutside && _isClickOutsideDialog(e, dialog)) {
      if (typeof onDismiss === "function") onDismiss();
      else if (dialog.open) dialog.close();
    }
    pressedOutside = false;
  });
}

function openNativeDialog(dialog) {
  if (!dialog) return;
  if (typeof dialog.showModal === "function") {
    if (!dialog.open) dialog.showModal();
  } else {
    dialog.classList.remove("hidden");
  }
}

function closeNativeDialog(dialog) {
  if (!dialog) return;
  if (typeof dialog.close === "function") {
    if (dialog.open) dialog.close();
  } else {
    dialog.classList.add("hidden");
  }
}

let _modalSeq = 0;

function appModal(options = {}) {
  const {
    title = "确认操作",
    message = "",
    content = "",
    actions = null,
    variant = "default",
    input = null,
    defaultValue = "",
    autofocus = true,
    width = "460px",
    exportText = "",
    exportFileName = "",
  } = options;
  return new Promise((resolve) => {
    const titleId = `app-modal-title-${++_modalSeq}`;
    const dialog = document.createElement("dialog");
    dialog.className = "app-modal-dialog";
    dialog.setAttribute("aria-labelledby", titleId);
    dialog.setAttribute("closedby", "any");
    const actionList = actions || [
      { label: "取消", value: false, kind: "secondary" },
      { label: "确认", value: true, kind: "primary" },
    ];
    const inputTag = input && input.type === "textarea"
      ? `<textarea class="app-modal-input" rows="${Number(input.rows || 6)}" placeholder="${escapeHtml(input.placeholder || "")}">${escapeHtml(defaultValue)}</textarea>`
      : `<input class="app-modal-input" type="${escapeHtml(input?.type || "text")}" value="${escapeHtml(defaultValue)}" placeholder="${escapeHtml(input?.placeholder || "")}" />`;
    const inputHtml = input ? `
      <label class="app-modal-field">
        ${input.label ? `<span>${escapeHtml(input.label)}</span>` : ""}
        ${inputTag}
      </label>
    ` : "";
    const hasCustomContent = !!content;
    const contentHtml = content instanceof HTMLElement ? "" : String(content || "");
    dialog.innerHTML = `
      <section class="app-modal app-modal--${escapeHtml(variant)}" style="width:min(calc(100vw - 32px), ${escapeHtml(width)})">
        <div class="app-modal-head">
          <h3 id="${titleId}">${escapeHtml(title)}</h3>
          <button type="button" class="app-modal-close" aria-label="关闭">×</button>
        </div>
        ${message ? `<div class="app-modal-message">${escapeHtml(message).replace(/\n/g, "<br>")}</div>` : ""}
        ${exportText ? `<div class="app-modal-export"><button type="button" class="btn btn-secondary btn-sm" data-export-error>导出错误信息</button></div>` : ""}
        ${inputHtml}
        ${hasCustomContent ? `<div class="app-modal-content">${contentHtml}</div>` : ""}
        <div class="app-modal-actions">
          ${actionList.map((a, idx) => `<button type="button" class="btn btn-sm ${a.kind === "primary" ? "btn-primary" : a.kind === "danger" ? "btn-danger" : "btn-secondary"}" data-action="${idx}">${escapeHtml(a.label)}</button>`).join("")}
        </div>
      </section>
    `;
    document.body.appendChild(dialog);
    const contentBox = dialog.querySelector(".app-modal-content");
    if (content instanceof HTMLElement && contentBox) contentBox.appendChild(content);
    dialog.querySelector("[data-export-error]")?.addEventListener("click", () => {
      downloadTextFile(exportFileName || buildErrorExportName("aetherswap-error"), exportText);
      toast("错误信息已导出");
    });

    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      if (dialog.open && typeof dialog.close === "function") dialog.close();
      dialog.remove();
      resolve(value);
    };

    dialog.addEventListener("close", () => finish(false));
    bindDialogLightDismiss(dialog, () => finish(false));
    dialog.querySelector(".app-modal-close")?.addEventListener("click", () => finish(false));
    dialog.querySelectorAll("[data-action]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const action = actionList[Number(btn.dataset.action)];
        if (input && action.value !== false) {
          finish(dialog.querySelector(".app-modal-input")?.value ?? "");
        } else {
          finish(action.value);
        }
      });
    });
    dialog.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && input && !e.shiftKey && e.target?.tagName !== "TEXTAREA") {
        e.preventDefault();
        if (e.isComposing || e.keyCode === 229) return;
        finish(dialog.querySelector(".app-modal-input")?.value ?? "");
      }
    });

    if (typeof dialog.showModal === "function") {
      dialog.showModal();
    }
    if (autofocus) {
      const target = dialog.querySelector(".app-modal-input")
        || dialog.querySelector(".app-modal-actions .btn-secondary")
        || dialog.querySelector(".app-modal-actions button");
      target?.focus();
    }
    if (typeof options.onOpen === "function") options.onOpen(dialog, finish);
  });
}

function appConfirm(message, options = {}) {
  return appModal({
    title: options.title || "确认操作",
    message,
    variant: options.variant || "warning",
    actions: [
      { label: options.cancelText || "取消", value: false, kind: "secondary" },
      { label: options.confirmText || "确认", value: true, kind: options.danger ? "danger" : "primary" },
    ],
    width: options.width || "460px",
  });
}

function appPrompt(title, defaultValue = "", options = {}) {
  return appModal({
    title,
    message: options.message || "",
    input: { label: options.label || "", placeholder: options.placeholder || "", type: options.type || "text", rows: options.rows },
    defaultValue,
    actions: [
      { label: options.cancelText || "取消", value: false, kind: "secondary" },
      { label: options.confirmText || "确认", value: true, kind: "primary" },
    ],
    width: options.width || "440px",
    exportText: options.exportText || "",
    exportFileName: options.exportFileName || "",
  });
}

async function fetchJson(url, opts = {}) {
  const res = await fetch(url, {
    ...opts,
    cache: opts.cache ?? "no-store",
    headers: {
      "Content-Type": "application/json",
      ...(opts.headers || {}),
    },
  });
  const text = await res.text();
  let data = {};
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { raw: text };
    }
  }
  if (!res.ok) {
    const msg = (data && (data.error || data.message)) || res.statusText || "请求失败";
    throw new Error(msg);
  }
  return data;
}

async function loadRuntimeProfile() {
  try {
    const data = await fetchJson(API + "/runtime");
    window.RUNTIME_PROFILE = data.runtime || null;
  } catch {
    window.RUNTIME_PROFILE = null;
  }
  applyRuntimeUiHints();
  return window.RUNTIME_PROFILE;
}

function getRuntimeProfile() {
  return window.RUNTIME_PROFILE || {};
}

function runtimeCanLaunchBrowser() {
  const profile = getRuntimeProfile();
  return profile.can_launch_headful_browser !== false;
}

function runtimeManualLoginMessage(type = "steam") {
  const label = type === "buff" ? "Buff" : "Steam";
  const profile = getRuntimeProfile();
  if (profile.can_launch_headful_browser === false) {
    return `当前后端运行在服务器/无桌面环境，无法弹出 ${label} 登录浏览器。请在你本机浏览器完成登录后，复制 ${label} Cookie 并粘贴保存。`;
  }
  return `请从已登录的浏览器复制 ${label} Cookie 后粘贴到下方。`;
}

function compactErrorText(reason, limit = 260) {
  let text = String(reason || "").replace(/\s+/g, " ").trim();
  if (!text) return "";
  text = text
    .replace(/Browser logs:.*/i, "完整浏览器日志见调试日志")
    .replace(/<launching>.*$/i, "完整浏览器日志见调试日志")
    .replace(/Call log:.*/i, "完整浏览器日志见调试日志");
  if (text.length > limit) {
    return text.slice(0, limit) + "…（完整信息可导出查看）";
  }
  return text;
}

function compactLoginError(reason) {
  return compactErrorText(reason, 220);
}

function buildErrorExportName(prefix = "aetherswap-error") {
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  return `${prefix}-${stamp}.txt`;
}

function downloadTextFile(filename, text) {
  const blob = new Blob([String(text || "")], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename || buildErrorExportName();
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function applyRuntimeUiHints() {
  const canLaunch = runtimeCanLaunchBrowser();
  const reloginOpen = el("relogin-btn-open");
  const reloginOk = el("relogin-btn-ok");
  if (reloginOpen) reloginOpen.textContent = canLaunch ? "打开浏览器并登录" : "手动填写 Cookie";
  if (reloginOk && !canLaunch) reloginOk.disabled = true;

  const wizardBuffOpen = el("wiz-buff-open");
  if (wizardBuffOpen) {
    const icon = wizardBuffOpen.querySelector("svg")?.outerHTML || "";
    wizardBuffOpen.innerHTML = `${icon}${canLaunch ? "打开浏览器登录 Buff" : "手动填写 Buff Cookie"}`;
  }
  const wizardBuffDesc = document.querySelector("#wizard-step-3 .wizard-desc");
  if (wizardBuffDesc && !canLaunch) {
    wizardBuffDesc.textContent = "AetherSwap 需要您的 Buff Cookie 来读取市场数据和下单。当前后端无法弹出图形浏览器，请在本机浏览器登录 Buff 后复制 Cookie 并粘贴保存。";
  }
  const accountGuideStep = document.querySelector("#accounts-guide-callout .agu-step:nth-child(2)");
  if (accountGuideStep && !canLaunch) {
    accountGuideStep.innerHTML = '<span class="agu-num">2</span> 点击账号卡片上的「<strong>验证</strong>」，若服务器无法自动处理则手动粘贴 Cookie';
  }
}

function buildAccountAvatar(name, avatarUrl, size = 40) {
  const initial = (name || "?").trim().charAt(0).toUpperCase() || "?";
  if (avatarUrl) {
    return `<div class="account-avatar-wrap" style="width:${size}px;height:${size}px">
      <img class="account-avatar" style="width:${size}px;height:${size}px" src="${escapeHtml(avatarUrl)}" alt="" data-img-fallback="next" />
      <div class="account-avatar placeholder" style="display:none;width:${size}px;height:${size}px">${escapeHtml(initial)}</div>
    </div>`;
  }
  return `<div class="account-avatar placeholder" style="width:${size}px;height:${size}px">${escapeHtml(initial)}</div>`;
}

function deepMerge(a, b) {
  const out = { ...a };
  for (const k of Object.keys(b)) {
    if (b[k] != null && typeof b[k] === "object" && !Array.isArray(b[k]) && typeof a[k] === "object" && a[k] != null) {
      out[k] = deepMerge(a[k], b[k]);
    } else {
      out[k] = b[k];
    }
  }
  return out;
}

function animateValue(elem, end, duration = 600) {
  if (!elem) return;
  const text = elem.textContent || "0";
  const start = parseFloat(text.replace(/[^0-9.\-]/g, "")) || 0;
  if (Math.abs(start - end) < 0.005) { elem.textContent = end.toFixed(text.includes(".") ? (text.split(".")[1] || "").length || 2 : 0); return; }
  const decimals = text.includes(".") ? Math.max((text.split(".")[1] || "").length, 2) : 0;
  const startTime = performance.now();
  const step = (now) => {
    const t = Math.min((now - startTime) / duration, 1);
    const ease = 1 - Math.pow(1 - t, 3);
    elem.textContent = (start + (end - start) * ease).toFixed(decimals);
    if (t < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

function steamItemImageUrl(icon, size = 62) {
  const path = String(icon || "").trim();
  if (!path) return "";
  const px = Math.max(16, Math.min(360, Number(size) || 62));
  return "https://community.cloudflare.steamstatic.com/economy/image/" + encodeURI(path) + "/" + px + "fx" + px + "f";
}

function formatWear(value) {
  if (value === null || value === undefined || value === "" || typeof value === "boolean") return "";
  const n = Number(value);
  if (!Number.isFinite(n) || n < 0 || n > 1) return "";
  return n.toFixed(8).replace(/0+$/, "").replace(/\.$/, "");
}

function buildItemMetaLine(typeText, exteriorText, wearValue) {
  return [typeText, exteriorText, formatWear(wearValue)]
    .map((part) => String(part || "").trim())
    .filter(Boolean)
    .join(" · ");
}

function itemThumbCellHtml(iconUrl) {
  const src = steamItemImageUrl(iconUrl, 62);
  if (!src) return '<td class="item-thumb-cell"></td>';
  return `<td class="item-thumb-cell"><img class="item-thumb" loading="lazy" decoding="async" src="${escapeHtml(src)}" alt="" data-img-fallback="hide"></td>`;
}

function formatDateTime(ts) {
  const d = ts instanceof Date ? ts : new Date(ts);
  if (!d || isNaN(d.getTime())) return "—";
  const pad = (v) => String(v).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function formatMoney(value, currency = "") {
  const n = Number(value);
  if (value == null || !Number.isFinite(n)) return "—";
  const formatted = n.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  if (!currency) return formatted;
  return currency === "CNY" ? `¥${formatted}` : `${currency} ${formatted}`;
}

function debounce(fn, wait = 300) {
  let timer = null;
  return function (...args) {
    if (timer !== null) clearTimeout(timer);
    timer = setTimeout(() => {
      timer = null;
      fn.apply(this, args);
    }, wait);
  };
}

document.addEventListener("error", (e) => {
  const target = e.target;
  if (!(target instanceof HTMLImageElement)) return;
  const mode = target.dataset.imgFallback;
  if (mode === "next") {
    target.style.display = "none";
    const next = target.nextElementSibling;
    if (next) next.style.display = "flex";
  } else if (mode === "hide") {
    target.style.visibility = "hidden";
  } else if (mode === "none") {
    target.style.display = "none";
  }
}, true);
