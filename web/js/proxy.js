let _proxyList = [];
let _proxyLoadState = "idle";
let _proxyDirty = false;
let _proxyPanelBound = false;

function _setButtonLabel(btn, text) {
    if (!btn) return;
    const label = btn.querySelector(".btn-label");
    if (label) label.textContent = text;
    else btn.textContent = text;
}

function _setProxyActionsDisabled(disabled) {
    for (const id of ["btn-proxy-save", "btn-proxy-test", "btn-proxy-clear", "btn-proxy-webshare"]) {
        const node = el(id);
        if (node) node.disabled = !!disabled;
    }
}

function _showProxyLoadError(message) {
    const banner = el("proxy-load-error");
    const msg = el("proxy-load-error-msg");
    if (msg) msg.textContent = message || "加载代理池配置失败，已锁定保存以防误清空。";
    if (banner) banner.classList.toggle("hidden", !message);
}

function proxyUrl(entry) {
    if (entry.username && entry.password) {
        return `${entry.host}:${entry.port} (${entry.username})`;
    }
    return `${entry.host}:${entry.port}`;
}

async function loadProxyConfig(force = false) {
    if (!force && _proxyDirty && _proxyLoadState === "loaded") return;
    try {
        const d = await fetchJson(API + "/proxy/config");
        const cfg = d.proxy_pool || {};
        _proxyList = (cfg.proxies || []).map(p => ({ ...p }));
        _proxyLoadState = "loaded";
        _proxyDirty = false;
        _showProxyLoadError("");
        _setProxyActionsDisabled(false);
        const strategy = cfg.strategy ?? 1;
        selectStrategy(strategy, false);
        const testUrlEl = el("proxy-test-url");
        if (testUrlEl) testUrlEl.value = cfg.test_url || "https://ipv4.webshare.io/";
        const timeoutEl = el("proxy-timeout");
        if (timeoutEl) timeoutEl.value = cfg.timeout_seconds ?? 10;
        const wsKeyEl = el("proxy-webshare-apikey");
        if (wsKeyEl) {
            const rawKey = cfg.webshare_api_key || "";
            if (rawKey === REDACTED_SECRET_SENTINEL) {
                wsKeyEl.value = "";
                wsKeyEl.dataset.configured = "1";
                wsKeyEl.placeholder = "已配置 API Key，留空保持不变";
            } else {
                wsKeyEl.value = rawKey;
                wsKeyEl.dataset.configured = rawKey ? "1" : "";
                wsKeyEl.placeholder = "填入后可点击「获取订阅」自动导入";
            }
        }
        renderProxyList();
    } catch (e) {
        console.error("加载代理配置失败", e);
        _proxyLoadState = "error";
        _showProxyLoadError(`加载代理配置失败（${e.message || "网络异常"}），已禁用保存避免覆盖服务端配置。`);
        _setProxyActionsDisabled(true);
    }
}

function renderProxyList(testResults) {
    const tbody = el("proxy-list-tbody");
    if (!tbody) return;
    const resultMap = {};
    if (testResults) {
        testResults.forEach(r => { resultMap[`${r.host}:${r.port}`] = r; });
    }
    if (_proxyList.length === 0) {
        tbody.innerHTML = `
      <tr>
        <td colspan="7" class="table-empty-cell">
          暂无代理，请添加
        </td>
      </tr>`;
        return;
    }
    tbody.innerHTML = _proxyList.map((p, i) => {
        const key = `${p.host}:${p.port}`;
        const r = resultMap[key];
        let statusHtml = `<span class="proxy-badge proxy-badge--idle">未测试</span>`;
        if (r) {
            if (r.status === "ok") {
                statusHtml = `<span class="proxy-badge proxy-badge--ok">✓ 正常</span>`;
            } else {
                statusHtml = `<span class="proxy-badge proxy-badge--fail" title="${escapeHtml(r.error || "")}">✗ 失败</span>`;
            }
        }
        const latency = r && r.status === "ok"
            ? `<span class="proxy-latency">${escapeHtml(String(r.latency_ms))} ms</span>`
            : `<span style="opacity:.45">—</span>`;
        const detectedIp = r && r.ip_detected
            ? `<code class="proxy-detected-ip">${escapeHtml(r.ip_detected)}</code>`
            : `<span style="opacity:.45">—</span>`;
        return `<tr data-idx="${i}">
      <td><span class="proxy-index">#${i + 1}</span></td>
      <td><code>${escapeHtml(p.host)}:${escapeHtml(String(p.port))}</code></td>
      <td><span style="opacity:.7">${escapeHtml(p.username || "—")}</span></td>
      <td>${statusHtml}</td>
      <td>${latency}</td>
      <td>${detectedIp}</td>
      <td>
        <button type="button" class="btn btn-sm btn-danger-outline" data-remove-proxy="${i}">删除</button>
      </td>
    </tr>`;
    }).join("");
}

function addSingleProxy() {
    if (_proxyLoadState === "error") {
        toast("请先重试加载配置", "配置尚未加载成功");
        return;
    }
    const host = (el("proxy-add-host")?.value || "").trim();
    const port = parseInt(el("proxy-add-port")?.value || "0", 10);
    const user = (el("proxy-add-user")?.value || "").trim();
    const pass = (el("proxy-add-pass")?.value || "").trim();
    if (!host || !port) {
        toast("请填写主机和端口", "主机和端口为必填项", 3500, "warn");
        return;
    }
    _proxyList.push({ host, port, username: user, password: pass });
    _proxyDirty = true;
    renderProxyList();
    ["proxy-add-host", "proxy-add-port", "proxy-add-user", "proxy-add-pass"].forEach(id => {
        const node = el(id);
        if (node) node.value = "";
    });
    toast("已添加", `${host}:${port}`, 3500, "success");
}

function parseBulkProxyImport() {
    if (_proxyLoadState === "error") {
        toast("请先重试加载配置", "配置尚未加载成功");
        return;
    }
    const raw = (el("proxy-bulk-input")?.value || "").trim();
    if (!raw) { toast("请输入代理列表", "", 3500, "warn"); return; }
    const lines = raw.split(/[\n\r]+/).map(l => l.trim()).filter(Boolean);
    let added = 0;
    const errors = [];
    lines.forEach((line, i) => {
        const parts = line.split(":");
        if (parts.length < 2) {
            errors.push(`行${i + 1}: 格式错误`);
            return;
        }
        const host = parts[0].trim();
        const port = parseInt(parts[1], 10);
        const username = parts[2]?.trim() || "";
        const password = parts[3]?.trim() || "";
        if (!host || isNaN(port)) {
            errors.push(`行${i + 1}: IP 或端口无效`);
            return;
        }
        const dup = _proxyList.some(p => p.host === host && p.port === port);
        if (dup) return;
        _proxyList.push({ host, port, username, password });
        added++;
    });
    if (added > 0) _proxyDirty = true;
    renderProxyList();
    const bulkEl = el("proxy-bulk-input");
    if (bulkEl) bulkEl.value = "";
    if (errors.length > 0) {
        toast(`已导入 ${added} 条，${errors.length} 条失败`, errors.slice(0, 3).join("; "), 4500, "warn");
    } else {
        toast(`已导入 ${added} 条代理`, "", 3500, "success");
    }
}

function removeProxy(idx) {
    if (!Number.isInteger(idx) || idx < 0 || idx >= _proxyList.length) return;
    _proxyList.splice(idx, 1);
    _proxyDirty = true;
    renderProxyList();
}

async function testAllProxies() {
    if (_proxyLoadState !== "loaded") {
        toast("无法测试", "请先等待代理配置加载完成", 3500, "warn");
        return;
    }
    if (_proxyList.length === 0) {
        toast("代理列表为空", "请先添加代理 IP", 3500, "warn");
        return;
    }
    const btn = el("btn-proxy-test");
    if (btn) {
        btn.disabled = true;
        _setButtonLabel(btn, "测试中...");
    }
    try {
        await _doSaveProxyConfig();
        const d = await fetchJson(API + "/proxy/test", { method: "POST" });
        if (d.ok === false) {
            toast("测试失败", d.error || "检测目标地址无效", 4500, "error");
            return;
        }
        renderProxyList(d.results || []);
        const ok = (d.results || []).filter(r => r.status === "ok").length;
        const fail = (d.results || []).length - ok;
        toast(`测试完成：${ok} 成功 / ${fail} 失败`, "", 3500, fail ? "warn" : "success");
    } catch (e) {
        toast("测试失败", e.message || "请检查后端日志", 4500, "error");
    } finally {
        if (btn) {
            btn.disabled = _proxyLoadState !== "loaded";
            _setButtonLabel(btn, "测试连通性");
        }
    }
}

async function _doSaveProxyConfig() {
    if (_proxyLoadState !== "loaded") {
        throw new Error("代理配置尚未成功加载，已阻止保存");
    }
    const strategy = Number(
        document.querySelector(".proxy-strategy-card.active")?.dataset?.strategy ?? 1
    );
    const enabled = strategy !== 3;
    const testUrl = el("proxy-test-url")?.value?.trim() || "https://ipv4.webshare.io/";
    const timeout = parseInt(el("proxy-timeout")?.value || "10", 10);
    const wsKeyEl = el("proxy-webshare-apikey");
    const typedKey = wsKeyEl?.value?.trim() || "";
    const webshareApiKey = typedKey || (wsKeyEl?.dataset?.configured === "1" ? REDACTED_SECRET_SENTINEL : "");
    const res = await fetchJson(API + "/proxy/config", {
        method: "POST",
        body: JSON.stringify({
            proxy_pool: {
                enabled,
                strategy,
                test_url: testUrl,
                timeout_seconds: timeout,
                webshare_api_key: webshareApiKey,
                proxies: _proxyList,
            }
        })
    });
    if (res && res.ok === false) {
        throw new Error(res.error || "配置校验未通过");
    }
    _proxyDirty = false;
}

async function saveProxyConfig() {
    try {
        await _doSaveProxyConfig();
        toast("代理池配置已保存", "", 3500, "success");
    } catch (e) {
        toast("保存失败", e.message || "请检查后端日志", 4500, "error");
    }
}

function selectStrategy(strategyId, markDirty = true) {
    document.querySelectorAll(".proxy-strategy-card").forEach(card => {
        const active = Number(card.dataset.strategy) === strategyId;
        card.classList.toggle("active", active);
        card.setAttribute("aria-pressed", active ? "true" : "false");
    });
    if (markDirty) _proxyDirty = true;
}

async function clearAllProxies() {
    if (_proxyLoadState !== "loaded") return;
    if (!(await appConfirm(`确定要清空所有 ${_proxyList.length} 个代理吗？此操作不可撤销。`, { title: "清空代理", danger: true, confirmText: "清空" }))) return;
    const btn = el("btn-proxy-clear");
    if (btn) { btn.disabled = true; _setButtonLabel(btn, "清除中..."); }
    try {
        const d = await fetchJson(API + "/proxy/clear", { method: "POST" });
        if (d.ok) {
            _proxyList = [];
            _proxyDirty = false;
            renderProxyList();
            toast("已清空代理列表", "", 3500, "success");
        } else {
            toast("清除失败", d.message || d.error || "", 4500, "error");
        }
    } catch (e) {
        toast("清除失败", e.message || "", 4500, "error");
    } finally {
        if (btn) { btn.disabled = _proxyLoadState !== "loaded"; _setButtonLabel(btn, "清除全部"); }
    }
}

async function fetchWebshareProxies() {
    if (_proxyLoadState !== "loaded") return;
    const wsKeyEl = el("proxy-webshare-apikey");
    const apiKey = (wsKeyEl?.value || "").trim() || (wsKeyEl?.dataset?.configured === "1" ? REDACTED_SECRET_SENTINEL : "");
    if (!apiKey) {
        toast("请先填写 Webshare API Key", "在测试参数区域输入后再点击获取", 3500, "warn");
        return;
    }
    const btn = el("btn-proxy-webshare");
    if (btn) { btn.disabled = true; _setButtonLabel(btn, "获取中..."); }
    try {
        await _doSaveProxyConfig();
        const d = await fetchJson(API + "/proxy/webshare", { method: "POST" });
        if (d.ok) {
            toast("获取成功", d.message || `已导入 ${d.count} 个代理`, 3500, "success");
            await loadProxyConfig(true);
        } else {
            toast("获取失败", d.message || d.error || "请检查 API Key 或账户状态", 4500, "error");
        }
    } catch (e) {
        toast("获取失败", e.message || "", 4500, "error");
    } finally {
        if (btn) { btn.disabled = _proxyLoadState !== "loaded"; _setButtonLabel(btn, "获取订阅"); }
    }
}

function bindProxyEvents() {
    if (_proxyPanelBound) return;
    _proxyPanelBound = true;
    document.querySelectorAll(".proxy-strategy-card").forEach(card => {
        card.addEventListener("click", () => selectStrategy(Number(card.dataset.strategy)));
    });
    el("btn-proxy-retry-load")?.addEventListener("click", () => loadProxyConfig(true));
    el("btn-proxy-webshare")?.addEventListener("click", fetchWebshareProxies);
    el("btn-proxy-clear")?.addEventListener("click", clearAllProxies);
    el("btn-proxy-test")?.addEventListener("click", testAllProxies);
    el("btn-proxy-add")?.addEventListener("click", addSingleProxy);
    el("btn-proxy-bulk-import")?.addEventListener("click", parseBulkProxyImport);
    el("btn-proxy-save")?.addEventListener("click", saveProxyConfig);
    el("proxy-list-tbody")?.addEventListener("click", (e) => {
        const btn = e.target.closest("[data-remove-proxy]");
        if (!btn) return;
        removeProxy(parseInt(btn.dataset.removeProxy, 10));
    });
    for (const id of ["proxy-test-url", "proxy-timeout", "proxy-webshare-apikey"]) {
        el(id)?.addEventListener("input", () => { _proxyDirty = true; });
    }
}
