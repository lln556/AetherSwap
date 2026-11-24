let holdingsMultiSelectMode = false;
let lastEnrichTime = 0;
let lastResellRatio = 0.85;
let lastEnrichData = null;

function marketPriceCoverageLabel(label, known, total) {
  return `${label}（${known === total ? `${total}件` : `${known}/${total}件`}）`;
}

function missingMarketPriceNote(purchaseCount, currentCount, total, comparableCount) {
  const notes = [];
  if (purchaseCount < total) notes.push(`缺少 ${total - purchaseCount} 件购入市场价`);
  if (currentCount < total) notes.push(`缺少 ${total - currentCount} 件当前市场价`);
  if (!notes.length) return "";
  return `<span class="summary-note">${notes.join("；")}。市场变动仅比较两种价格都有记录的 ${comparableCount} 件。</span>`;
}

function renderTxTable(tbody, list, isPurchase = false, resellRatio = 0.85, multiSelectMode = false) {
  const ratio = Math.max(0.01, Math.min(1, Number(resellRatio) || 0.85));
  if (!list.length) {
    const colSpan = isPurchase ? (multiSelectMode ? 14 : 13) : 5;
    tbody.innerHTML = `<tr><td colspan="${colSpan}" class="table-empty-cell">暂无持有饰品记录</td></tr>`;
    return;
  }
  const rowHtmls = [];
  for (const t of list) {
    const at = t.at ? new Date(t.at * 1000) : null;
    const timeStr = at ? formatDateTime(at) : "—";
    const nameText = (t.name || "—").toString();
    const metaLine = buildItemMetaLine(t.item_type, t.exterior, t.paintwear);
    const nameCell = `<td>${escapeHtml(nameText)}${metaLine ? `<div class="item-sub">${escapeHtml(metaLine)}</div>` : ""}</td>`;
    const idx = t.idx;
    const type = t.type;
    const actHtml = isPurchase
      ? (multiSelectMode
        ? `<button type="button" class="btn btn-sm btn-edit tx-btn-edit" data-type="${escapeHtml(type)}" data-idx="${idx}">编辑</button>`
        : `<button type="button" class="btn btn-sm btn-edit tx-btn-edit" data-type="${escapeHtml(type)}" data-idx="${idx}">编辑</button> <button type="button" class="btn btn-sm btn-primary tx-btn-sell" data-type="${escapeHtml(type)}" data-idx="${idx}">售出</button> <button type="button" class="btn btn-sm btn-danger-outline tx-btn-del" data-type="${escapeHtml(type)}" data-idx="${idx}" data-db-id="${Number(t.db_id) || 0}">删除</button>`)
      : `<button type="button" class="btn btn-sm btn-edit tx-btn-edit" data-type="${escapeHtml(type)}" data-idx="${idx}">编辑</button> <button type="button" class="btn btn-sm btn-danger-outline tx-btn-del" data-type="${escapeHtml(type)}" data-idx="${idx}" data-db-id="${Number(t.db_id) || 0}">删除</button>`;
    const checkCell = isPurchase && multiSelectMode ? `<td class="holding-select-cell"><input type="checkbox" class="holding-checkbox" data-idx="${idx}" data-db-id="${Number(t.db_id) || 0}" aria-label="选择 ${escapeHtml(nameText)}" /></td>` : "";
    const priceCell = `<td class="mono">${escapeHtml(Number(t.price).toFixed(2))}</td>`;
    if (isPurchase) {
      const mp = t.market_price != null ? Number(t.market_price).toFixed(2) : "未记录";
      const cur = t.current_market_price != null ? Number(t.current_market_price) : null;
      const cmp = cur != null ? cur.toFixed(2) : "";
      const marketAtBuy = t.market_price != null ? Number(t.market_price) : null;
      let plCell = "<td></td>";
      if (cur != null && marketAtBuy != null && marketAtBuy > 0) {
        const diff = cur - marketAtBuy;
        const pct = ((diff / marketAtBuy) * 100).toFixed(2) + "%";
        const cls = diff > 0 ? "text-ok" : diff < 0 ? "text-bad" : "";
        plCell = `<td class="mono ${cls}">${diff >= 0 ? "+" : ""}${diff.toFixed(2)} (${diff >= 0 ? "+" : ""}${pct})</td>`;
      }
      const cost = Number(t.price) || 0;
      const afterTaxVal = cur != null && cur > 0 ? cur / 1.15 : null;
      const afterTax = afterTaxVal != null ? afterTaxVal.toFixed(2) : "";
      const discountRatio = afterTaxVal != null && afterTaxVal > 0 && cost > 0 ? (cost / afterTaxVal).toFixed(4) : "";
      const discountRatioClass = discountRatio ? (parseFloat(discountRatio) > ratio ? "text-bad" : "text-ok") : "";
      const cashProfit = afterTaxVal != null && cost > 0 ? (afterTaxVal * ratio - cost).toFixed(2) : "";
      const profitClass = cashProfit ? (parseFloat(cashProfit) > 0 ? "text-ok" : parseFloat(cashProfit) < 0 ? "text-bad" : "") : "";
      const selfUseProfit = afterTaxVal != null && cost > 0 ? (afterTaxVal - cost).toFixed(2) : "";
      const selfUseClass = selfUseProfit ? (parseFloat(selfUseProfit) > 0 ? "text-ok" : parseFloat(selfUseProfit) < 0 ? "text-bad" : "") : "";
      const afterTaxCell = afterTax ? `<td class="mono">${escapeHtml(afterTax)}</td>` : "<td></td>";
      const discountRatioCell = discountRatio ? `<td class="mono ${discountRatioClass}">${escapeHtml(discountRatio)}</td>` : "<td></td>";
      const profitCell = cashProfit ? `<td class="mono ${profitClass}">${escapeHtml(parseFloat(cashProfit) >= 0 ? "+" + cashProfit : cashProfit)}</td>` : "<td></td>";
      const selfUseCell = selfUseProfit ? `<td class="mono ${selfUseClass}">${escapeHtml(parseFloat(selfUseProfit) >= 0 ? "+" + selfUseProfit : selfUseProfit)}</td>` : "<td></td>";
      const assetidCell = `<td class="mono">${escapeHtml(t.assetid ?? "—")}</td>`;
      rowHtmls.push(`<tr>${checkCell}${itemThumbCellHtml(t.icon_url)}<td class="mono">${escapeHtml(timeStr)}</td>${nameCell}${assetidCell}${priceCell}<td class="mono">${escapeHtml(mp)}</td><td class="mono">${escapeHtml(cmp)}</td>${afterTaxCell}${discountRatioCell}${profitCell}${selfUseCell}${plCell}<td class="tx-actions">${actHtml}</td></tr>`);
    } else {
      const assetidCell = `<td class="mono">${escapeHtml(t.assetid ?? "—")}</td>`;
      rowHtmls.push(`<tr>${checkCell}<td class="mono">${escapeHtml(timeStr)}</td><td>${escapeHtml(nameText)}</td>${assetidCell}${priceCell}<td class="tx-actions">${actHtml}</td></tr>`);
    }
  }
  tbody.innerHTML = rowHtmls.join("");
  tbody.querySelectorAll(".tx-btn-del").forEach(btn => {
    btn.addEventListener("click", async () => {
      if (!(await appConfirm("确定归档这条记录？交易中心将保留历史。", { title: "归档记录", danger: true, confirmText: "归档" }))) return;
      const type = btn.dataset.type;
      const idx = parseInt(btn.dataset.idx, 10);
      try {
        const dbId = parseInt(btn.dataset.dbId, 10) || 0;
        const params = { type, idx };
        if (dbId) params.db_id = dbId;
        const r = await fetchJson(API + "/transaction?" + new URLSearchParams(params), { method: "DELETE" });
        if (r.ok) {
          toast("已删除");
          refreshTransactions();
        } else {
          toast("删除失败", r.error || "", 3500, "error");
        }
      } catch (e) {
        toast("删除失败", e.message || "", 3500, "error");
      }
    });
  });
  tbody.querySelectorAll(".tx-btn-edit").forEach(btn => {
    btn.addEventListener("click", () => {
      const type = btn.dataset.type;
      const idx = parseInt(btn.dataset.idx, 10);
      const t = list.find(x => x.type === type && x.idx === idx);
      if (!t) return;
      el("edit-tx-name").value = t.name || "";
      el("edit-tx-price").value = t.price ?? "";
      el("edit-tx-goods-id").value = t.goods_id ?? "";
      const mpWrap = el("edit-tx-market-price-wrap");
      if (mpWrap) mpWrap.style.display = type === "purchase" ? "" : "none";
      const mpEl = el("edit-tx-market-price");
      if (mpEl) mpEl.value = type === "purchase" ? (t.market_price ?? "") : "";
      const assetidWrap = el("edit-tx-assetid-wrap");
      if (assetidWrap) assetidWrap.style.display = type === "purchase" ? "" : "none";
      const assetidEl = el("edit-tx-assetid");
      if (assetidEl) assetidEl.value = type === "purchase" ? (t.assetid ?? "") : "";
      const listingWrap = el("edit-tx-listing-wrap");
      if (listingWrap) listingWrap.style.display = type === "purchase" ? "" : "none";
      const listingEl = el("edit-tx-listing");
      if (listingEl) {
        if (type !== "purchase") listingEl.value = "0";
        else if (t.pending_receipt) listingEl.value = "2";
        else if (t.listing) listingEl.value = "1";
        else listingEl.value = "0";
      }
      const ov = el("edit-tx-overlay");
      ov.dataset.editType = type;
      ov.dataset.editIdx = String(idx);
      ov.dataset.editDbId = String(Number(t.db_id) || 0);
      openNativeDialog(ov);
    });
  });
  tbody.querySelectorAll(".tx-btn-sell").forEach(btn => {
    btn.addEventListener("click", () => {
      const idx = parseInt(btn.dataset.idx, 10);
      const t = list.find(x => x.type === "purchase" && x.idx === idx);
      if (!t) return;
      const nameEl = el("sell-tx-item-name");
      if (nameEl) nameEl.textContent = t.name || "—";
      const ov = el("sell-tx-overlay");
      ov.dataset.sellIdx = String(idx);
      ov.dataset.sellDbId = String(Number(t.db_id) || 0);
      delete ov.dataset.sellIdxList;
      const priceEl = el("sell-tx-price");
      if (priceEl) priceEl.value = "";
      openNativeDialog(ov);
      if (priceEl) priceEl.focus();
    });
  });
}

function applyTransactionsToUI(all, summaryEl, tbodyP, resellRatio = 0.85) {
  const purchases = all.filter((t) => t.type === "purchase");
  const holdings = purchases.filter((t) => !(t.sale_price != null && Number(t.sale_price) > 0));
  const ratio = Math.max(0.01, Math.min(1, Number(resellRatio) || 0.85));
  if (tbodyP) renderTxTable(tbodyP, holdings, true, ratio, holdingsMultiSelectMode);
  if (summaryEl && holdings.length) {
    const totalPrice = holdings.reduce((s, t) => s + (Number(t.price) || 0), 0);
    const totalMp = holdings.reduce((s, t) => s + (t.market_price != null ? Number(t.market_price) : 0), 0);
    const purchaseMarketCount = holdings.filter(t => t.market_price != null).length;
    const currentMarketCount = holdings.filter(t => t.current_market_price != null).length;
    const hasAnyCmp = holdings.some((t) => t.current_market_price != null);
    const totalCmp = hasAnyCmp ? holdings.reduce((s, t) => s + (t.current_market_price != null ? Number(t.current_market_price) : 0), 0) : null;
    const marketChangeTotals = holdings.reduce((acc, t) => {
      const cmp = t.current_market_price != null ? Number(t.current_market_price) : null;
      const mp = t.market_price != null ? Number(t.market_price) : null;
      if (cmp != null && Number.isFinite(cmp) && mp != null && Number.isFinite(mp) && mp > 0) {
        acc.current += cmp;
        acc.purchase += mp;
        acc.count += 1;
      }
      return acc;
    }, { current: 0, purchase: 0, count: 0 });
    const totalPl = marketChangeTotals.count > 0 ? marketChangeTotals.current - marketChangeTotals.purchase : null;
    const totalPlPct = totalPl != null && marketChangeTotals.purchase > 0 ? ((totalPl / marketChangeTotals.purchase) * 100).toFixed(2) + "%" : "—";
    const plClass = totalPl != null && totalPl > 0 ? "text-ok" : totalPl != null && totalPl < 0 ? "text-bad" : "";
    const cmpStr = totalCmp != null ? totalCmp.toFixed(2) : "—";
    const plStr = totalPl != null ? `${totalPl >= 0 ? "+" : ""}${totalPl.toFixed(2)} (${totalPl >= 0 ? "+" : ""}${totalPlPct})` : "—";
    const totalAfterTax = totalCmp != null && totalCmp > 0 ? totalCmp / 1.15 : null;
    const afterTaxStr = totalAfterTax != null ? totalAfterTax.toFixed(2) : "—";
    let ratioSum = 0, ratioCount = 0, totalCashProfit = 0, totalSelfUseProfit = 0;
    holdings.forEach((t) => {
      const cmp = t.current_market_price != null ? Number(t.current_market_price) : null;
      if (cmp == null || cmp <= 0) return;
      const afterTax = cmp / 1.15;
      const cost = Number(t.price) || 0;
      if (afterTax > 0 && cost > 0) { ratioSum += cost / afterTax; ratioCount += 1; }
      totalCashProfit += afterTax * ratio - cost;
      totalSelfUseProfit += afterTax - cost;
    });
    const discountRatio = ratioCount > 0 ? (ratioSum / ratioCount).toFixed(4) : "—";
    const discountRatioClass = discountRatio !== "—" ? (parseFloat(discountRatio) > ratio ? "text-bad" : "text-ok") : "";
    const cashProfitVal = ratioCount > 0 ? totalCashProfit : null;
    const selfUseProfitVal = ratioCount > 0 ? totalSelfUseProfit : null;
    const profitClass = cashProfitVal != null && cashProfitVal > 0 ? "text-ok" : cashProfitVal != null && cashProfitVal < 0 ? "text-bad" : "";
    const selfUseProfitClass = selfUseProfitVal != null && selfUseProfitVal > 0 ? "text-ok" : selfUseProfitVal != null && selfUseProfitVal < 0 ? "text-bad" : "";
    const cashProfitStr = cashProfitVal != null ? (cashProfitVal >= 0 ? "+" : "") + cashProfitVal.toFixed(2) : "—";
    const selfUseProfitStr = selfUseProfitVal != null ? (selfUseProfitVal >= 0 ? "+" : "") + selfUseProfitVal.toFixed(2) : "—";
    summaryEl.innerHTML = [
      `<span class="summary-stat"><span class="summary-label">数量</span><span class="summary-value mono">${holdings.length}</span></span>`,
      `<span class="summary-stat"><span class="summary-label">总购入价</span><span class="summary-value mono">${totalPrice.toFixed(2)}</span></span>`,
      `<span class="summary-stat"><span class="summary-label">${marketPriceCoverageLabel("总购入市场价", purchaseMarketCount, holdings.length)}</span><span class="summary-value mono">${purchaseMarketCount ? totalMp.toFixed(2) : "—"}</span></span>`,
      `<span class="summary-stat"><span class="summary-label">${marketPriceCoverageLabel("总现市场价", currentMarketCount, holdings.length)}</span><span class="summary-value mono">${cmpStr}</span></span>`,
      `<span class="summary-stat"><span class="summary-label">总税后价格</span><span class="summary-value mono">${afterTaxStr}</span></span>`,
      `<span class="summary-stat"><span class="summary-label">实际折扣比率</span><span class="summary-value mono ${discountRatioClass}">${discountRatio}</span></span>`,
      `<span class="summary-stat"><span class="summary-label">总变现收益</span><span class="summary-value mono ${profitClass}">${cashProfitStr}</span></span>`,
      `<span class="summary-stat"><span class="summary-label">总自用收益</span><span class="summary-value mono ${selfUseProfitClass}">${selfUseProfitStr}</span></span>`,
      `<span class="summary-stat"><span class="summary-label">总市场变动（${marketChangeTotals.count}件）</span><span class="summary-value mono ${plClass}">${plStr}</span></span>`,
      missingMarketPriceNote(purchaseMarketCount, currentMarketCount, holdings.length, marketChangeTotals.count),
    ].join("");
    summaryEl.style.display = "";
  } else if (summaryEl) {
    summaryEl.textContent = "";
    summaryEl.style.display = "none";
  }
  refreshAnalytics(purchases, ratio);
  syncHoldingsMultiSelectUI();
}

function syncHoldingsMultiSelectUI() {
  const selectTh = el("holding-select-th");
  const batchBar = el("holdings-batch-actions");
  const multiselectBtn = el("btn-holdings-multiselect");
  if (holdingsMultiSelectMode) {
    if (selectTh) selectTh.classList.remove("hidden");
    if (batchBar) { batchBar.classList.remove("hidden"); batchBar.style.display = "flex"; }
    if (multiselectBtn) multiselectBtn.textContent = "取消多选";
  } else {
    if (selectTh) selectTh.classList.add("hidden");
    if (batchBar) { batchBar.classList.add("hidden"); batchBar.style.display = "none"; }
    if (multiselectBtn) multiselectBtn.textContent = "多选";
  }
}

function getCurrentPriceRefreshMinutes() {
  return Math.max(1, parseInt(el("cfg-current-price-refresh-minutes")?.value, 10) || 10);
}

async function refreshTransactions() {
  const tbodyP = document.querySelector("#transactions-table-purchases tbody");
  const summaryEl = el("purchases-summary");
  if (!tbodyP) return;
  try {
    const d = await fetchJson(API + "/transactions?enrich_current_price=0");
    const all = d.transactions || [];
    const enrichedMap = new Map((lastEnrichData || []).filter(t => t.type === "purchase").map((t) => [t.name, t]));
    for (const t of all) {
      const e = t.type === "purchase" ? enrichedMap.get(t.name) : null;
      if (e && e.current_market_price != null) t.current_market_price = e.current_market_price;
    }
    lastEnrichData = all;
    lastResellRatio = d.resell_ratio;
    applyTransactionsToUI(all, summaryEl, tbodyP, lastResellRatio);
  } catch (e) {
    toast("加载操作记录失败", e.message || "", 3500, "error");
  }
}

function renderTransactionsFromCache() {
  const tbodyP = document.querySelector("#transactions-table-purchases tbody");
  if (!lastEnrichData || !tbodyP) return;
  const summaryEl = el("purchases-summary");
  applyTransactionsToUI(lastEnrichData, summaryEl, tbodyP, lastResellRatio);
}
