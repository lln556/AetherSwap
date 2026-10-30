const Dashboard = {
  actionPending: false,
  connected: false,
  lastStatus: null,
  setText(id, text) {
    const node = el(id);
    if (node) node.textContent = text;
  },
  money(value) {
    return Number.isFinite(Number(value)) && value != null
      ? Number(value).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : "—";
  },
  date(value) {
    return value ? new Date(Number(value) * 1000).toLocaleString("zh-CN", { hour12: false }) : "暂无记录";
  },
  connection(ok) {
    this.connected = ok;
    el("dashboard-connection")?.classList.toggle("hidden", ok);
    if (!ok) {
      this.setText("status-text", "连接中断");
      const pill = el("status-pill");
      pill?.classList.remove("status-running", "status-idle", "status-stopped");
      pill?.classList.add("status-error");
    }
    this.renderButtons();
  },
  renderButtons() {
    const d = this.lastStatus;
    const active = d && (d.pipeline_active || d.status === "running");
    const stopping = active && d.status === "stopped";
    const start = el("btn-start");
    const stop = el("btn-stop");
    if (start) {
      start.disabled = !this.connected || this.actionPending || !!active;
      start.classList.toggle("hidden", !!active);
      start.textContent = stopping ? "正在停止…" : active ? "任务运行中" : "启动任务";
    }
    if (stop) {
      stop.disabled = !this.connected || this.actionPending || !active || stopping;
      stop.classList.toggle("hidden", !active);
      stop.textContent = stopping ? "正在停止…" : "停止运行";
    }
  },
  renderStatus(d) {
    this.lastStatus = d;
    this.connection(true);
    const scan = d.scan_summary || {};
    const started = !!scan.started_at;
    this.setText("scan-started", started ? new Date(scan.started_at * 1000).toLocaleTimeString("zh-CN", { hour12: false }) + " 开始" : "等待扫描");
    for (const key of ["candidates", "checked", "passed"]) this.setText("scan-" + key, started ? String(scan[key] || 0) : "—");
    this.setText("scan-summary-hint", !started ? "启动任务后显示本轮筛选结果。" :
      "通过筛选后仍需核对下单条件，不代表已购买。" + (d.status !== "running" ? " 当前保留最近一轮结果。" : ""));
    const reasons = { discount: "成本率超过阈值", stability: "价格稳定性未通过", sell_pressure: "卖压过高", market_data: "卖单数据暂不可用", history_data: "历史数据不足或异常", custom_module: "策略模块未通过", buff_precheck: "BUFF 实时预检未通过" };
    const list = el("scan-reasons");
    if (list) {
      list.replaceChildren();
      const entries = Object.entries(scan.reasons || {}).filter(([, count]) => count > 0).sort((a, b) => b[1] - a[1]);
      for (const [reason, count] of entries) {
        const row = document.createElement("div");
        const label = document.createElement("span"); label.textContent = reasons[reason] || "其他条件未通过";
        const value = document.createElement("strong"); value.textContent = String(count);
        row.append(label, value); list.append(row);
      }
      if (!entries.length) {
        const hint = document.createElement("p"); hint.className = "dashboard-meta";
        hint.textContent = started ? "暂无未通过记录，检查结果会持续更新。" : "这里会汇总每件候选的最终筛选结果。";
        list.append(hint);
      }
    }
    const expiry = d.steam_session_expires_at;
    this.setText("health-session-note", d.steam_keepalive_enabled
      ? "会话接近到期时自动续期；需要重新登录时会提示。"
      : "自动续期未启用，请留意会话到期时间。");
    const expired = d.steam_auth_expired || (expiry && expiry * 1000 <= Date.now());
    this.setText("health-steam", expired ? "需要重新登录" : expiry ? "令牌到期 " + this.date(expiry) : "尚未取得有效期");
    el("health-steam")?.classList.toggle("text-bad", !!expired);
    const buffNeedsAction = d.buff_auth_expired || d.buff_verification_required || d.buff_no_cookie;
    this.setText("health-buff", d.buff_verification_required ? "需要安全验证" : d.buff_auth_expired ? "需要重新登录" : d.buff_no_cookie ? "尚未配置" : "凭据已配置");
    el("health-buff")?.classList.toggle("text-bad", !!buffNeedsAction);
    const region = d.account_region || {};
    this.setText("health-region", region.region_check_ok ? [region.currency_code, region.region_code].filter(Boolean).join(" / ") : "地区尚未确认");
    // BUFF session history has its own cache endpoint. Keep replacement
    // protection fresh without probing BUFF or conflating it with live flags.
    window.BuffSession?.refreshCachedSession?.();
  },
  renderStats(s) {
    this.setText("stat-holding-cost", this.money(s.holding_cost));
    this.setText("stat-holding-count", `未售 ${s.holding_count || 0} 件 · 含待收货，按购入价统计`);
    this.setText("stat-total-purchased", this.money(s.total_purchased));
    this.setText("stat-sold-count", `已售 ${s.sold_count || 0} 件 · 含已归档成交`);
    const amounts = Object.entries(s.proceeds_by_currency || {}).sort(([a], [b]) => a.localeCompare(b));
    this.setText("stat-proceeds", amounts.map(([currency, group]) => {
      const notes = [];
      if (group.estimated_count) notes.push(`${group.estimated_count} 笔历史估算`);
      if (group.unknown_count) notes.push(`${group.unknown_count} 笔金额未知`);
      return `${currency} ${group.unknown_count ? "已知到账" : "到账"} ${this.money(group.proceeds_minor == null ? null : group.proceeds_minor / 100)}${notes.length ? "（" + notes.join("，") + "）" : ""}`;
    }).join(" · ") || "暂无成交到账");
    const cny = s.proceeds_by_currency?.CNY;
    const profitNotes = ["仅 CNY 成交：到账 − 对应成本"];
    if (cny?.estimated_count) profitNotes.push(`含 ${cny.estimated_count} 笔历史估算`);
    if (cny?.unknown_count) profitNotes.push(`${cny.unknown_count} 笔到账未知未计入`);
    if (amounts.some(([currency]) => currency !== "CNY")) profitNotes.push("其他币种未折算");
    this.setText("stat-profit-note", profitNotes.join(" · "));
    this.setText("stat-diff", this.money(s.total_profit));
    el("stat-diff")?.classList.toggle("text-ok", s.total_profit > 0);
    el("stat-diff")?.classList.toggle("text-bad", s.total_profit < 0);
    this.setText("stat-ratio", s.discount_ratio != null ? Number(s.discount_ratio).toFixed(4) : "—");
    this.setText("dashboard-last-purchase", this.date(s.last_purchase_at));
    const list = el("dashboard-recent-list");
    if (list) {
      list.replaceChildren();
      for (const p of s.recent_purchases || []) {
        const row = document.createElement("div"); row.className = "dashboard-recent-row";
        const detail = document.createElement("div");
        const name = document.createElement("strong"); name.textContent = p.name;
        const time = document.createElement("span"); time.className = "dashboard-meta"; time.textContent = this.date(p.at);
        detail.append(name, time);
        const price = document.createElement("strong"); price.className = "dashboard-money"; price.textContent = this.money(p.price);
        const state = document.createElement("span"); state.className = "dashboard-tag"; state.textContent = p.sold ? "已售出" : p.state === "archived" ? "已归档" : p.pending_receipt ? "待收货" : "持有中";
        row.append(detail, price, state); list.append(row);
      }
      if (!list.children.length) {
        const empty = document.createElement("p"); empty.className = "dashboard-meta"; empty.textContent = "暂无买入记录。任务成交后会显示在这里。"; list.append(empty);
      }
    }
    this.setText("dashboard-stats-status", "");
  },
  bind() {
    document.querySelectorAll("[data-dashboard-tab]").forEach(button => button.addEventListener("click", () => tabSwitch(button.dataset.dashboardTab)));
    const overlay = el("mobile-more-overlay");
    const more = el("btn-nav-more");
    const close = () => {
      if (overlay?.classList.contains("hidden")) return;
      overlay?.classList.add("hidden"); more?.setAttribute("aria-expanded", "false"); more?.focus();
    };
    const primary = ["auto", "inventory", "purchases", "debug"];
    const links = el("mobile-more-links");
    document.querySelectorAll(".nav-menu [data-tab]").forEach(button => {
      button.classList.toggle("mobile-primary", primary.includes(button.dataset.tab));
      if (!primary.includes(button.dataset.tab)) {
        const copy = button.cloneNode(true); copy.removeAttribute("id");
        copy.querySelectorAll("[id]").forEach(node => node.removeAttribute("id"));
        copy.classList.remove("active"); copy.classList.add("mobile-menu-link");
        copy.addEventListener("click", async () => { if (await tabSwitch(copy.dataset.tab)) close(); }); links?.append(copy);
      }
    });
    more?.addEventListener("click", () => {
      overlay?.classList.remove("hidden"); more.setAttribute("aria-expanded", "true"); el("btn-nav-close")?.focus();
    });
    el("btn-nav-close")?.addEventListener("click", close);
    overlay?.addEventListener("click", e => { if (e.target === overlay) close(); });
    overlay?.addEventListener("keydown", e => {
      if (e.key === "Escape") { e.preventDefault(); close(); }
      if (e.key === "Tab") {
        const nodes = [...overlay.querySelectorAll("button")].filter(node => !node.disabled);
        const first = nodes[0], last = nodes[nodes.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last?.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first?.focus(); }
      }
    });
    const mobileQuery = window.matchMedia("(max-width: 768px)");
    mobileQuery.addEventListener("change", () => { close(); this.applyScale(); });
    el("btn-mobile-theme")?.addEventListener("click", () => Theme.cycle());
    document.addEventListener("click", e => {
      if (!e.target.closest(".strategy-more-actions")) document.querySelectorAll(".strategy-more-actions[open]").forEach(node => node.removeAttribute("open"));
    });
    document.querySelectorAll(".strategy-more-menu button").forEach(button => button.addEventListener("click", () => button.closest("details")?.removeAttribute("open")));
  },
  applyScale() {
    const scale = el("cfg-ui_scale")?.value || "1";
    document.documentElement.style.zoom = window.matchMedia("(max-width: 768px)").matches ? "1" : scale;
  },
  syncNavigation(name) {
    document.querySelectorAll(".mobile-menu-link").forEach(node => node.classList.toggle("active", node.dataset.tab === name));
    el("btn-nav-more")?.classList.toggle("active", !["auto", "inventory", "purchases", "debug"].includes(name));
  },
};
