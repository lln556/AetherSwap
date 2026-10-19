(() => {
  const labels = {pending_receipt:"待收货", holding:"持有中", submitted:"上架待核实", pending_confirmation:"待 Steam 确认", active:"出售中", repricing:"调价处理中", unknown:"状态待核实", attention:"需人工核实", sold:"已出售", cancelled:"已撤单", archived:"已归档"};
  const esc = value => escapeHtml(String(value ?? ""));
  const money = (n, currency="CNY") => n == null ? "—" : `${currency === "CNY" ? "¥" : currency + " "}${(n/100).toFixed(2)}`;
  const date = t => t ? new Date(t*1000).toLocaleString("zh-CN", {timeZone:"Asia/Shanghai", hour12:false}) : "—";
  const age = t => t ? `${Math.max(0,(Date.now()/1000-t)/3600).toFixed(1)} 小时` : "待核实";
  let page = 1, busy = false, hydrated = false, requestSeq = 0;
  const itemCell = r => `<td class="trading-item">${esc(r.name)}<small class="muted">#${r.id} · ${esc(r.assetid || "资产待收货")}</small></td>`;
  const evaluationLabel = (r, policy) => {
    if (r.job) {
      const stages = {planned:"等待执行调价", removing:"准备撤单，核实库存", awaiting_inventory:"核实撤单结果", ready:"等待重新上架", submitting:"核实上架请求", confirming:"等待新挂单确认", needs_review:"需要人工核实"};
      const retryAt = Math.max(r.job.retry_at || 0, ["planned", "removing", "ready"].includes(r.job.status) ? policy.mutation_retry_at || 0 : 0);
      return `${esc(stages[r.job.status] || "调价处理中")}<small class="muted">目标挂价 ${money(r.job.target_buyer_minor)} · 预计到账 ${money(r.job.target_seller_minor)}</small>${retryAt > Date.now()/1000 ? `<small class="muted">不早于 ${date(retryAt)} 重试</small>` : ""}<small class="muted">${esc(r.job.error || "将在下一轮市场核实中继续")}</small>`;
    }
    const l = r.listing || {};
    return `${date(l.next_evaluation_at)}<small class="muted">${esc(l.last_reason || (!policy.enabled ? "自动调价未开启" : l.next_evaluation_at*1000 <= Date.now() ? "已到期，等待评估" : "尚未到评估时间"))}</small>`;
  };
  const detailButton = r => `<button class="btn btn-sm btn-secondary" data-trading-detail="${r.id}">时间线</button>`;
  const saleRow = r => {
    const s = r.settlement, net = s.proceeds_minor;
    const actual = ["steam_history", "user_reported"].includes(s.amount_source);
    const profit = s.currency === "CNY" && net != null ? money(net-r.cost_minor) : "—";
    return `<tr><td>${esc(s.sold_day || s.date_label || "成交日期未知")}<small class="muted">${s.time_precision === "day" ? "仅日期" : "时间待核实"}</small></td>${itemCell(r)}<td>${money(r.cost_minor)}</td><td>${money(net,s.currency)}${actual ? "" : '<small class="muted">历史估算，待核实</small>'}</td><td>${profit}</td><td>${s.amount_source === "steam_history" ? "Steam 成交历史" : s.amount_source === "user_reported" ? "用户记账" : "旧记录迁移"}<small class="muted">发现于 ${date(s.detected_at)}</small></td><td>${detailButton(r)}</td></tr>`;
  };
  function showPolicy(p) {
    el("trading-policy-state").textContent = p.enabled ? "· 已启用，只降不涨" : "· 未启用";
    el("trading-sync-status").textContent = `最近核实：${date(p.last_check_at)} · 下次检查：${date(p.next_check_at)}${p.last_error ? " · " + p.last_error : ""}${p.mutation_retry_at > Date.now()/1000 ? " · 自动撤单/上架冷却至 " + date(p.mutation_retry_at) : ""}`;
    if (hydrated) return;
    el("trading-enabled").checked = p.enabled;
    el("trading-age").value = p.age_hours;
    el("trading-loss").value = p.max_loss_percent;
    el("trading-delta-pct").value = p.min_change_percent;
    el("trading-delta-amount").value = (p.min_change_minor/100).toFixed(2);
    hydrated = true;
  }
  window.refreshTrading = async function () {
    const seq = ++requestSeq;
    const view = el("trading-view")?.value || "purchases";
    const params = new URLSearchParams({days:el("trading-days")?.value || "0", status:el("trading-status")?.value || "", query:el("trading-query")?.value || "", page, page_size:50});
    try {
      const d = await fetchJson(API + "/trading?" + params);
      if (seq !== requestSeq) return;
      showPolicy(d.policy);
      const s=d.summary;
      el("trading-summary").innerHTML = [["今日购买",`${s.today_bought} 件`,money(s.today_cost_minor)],["今日成交",`${s.today_sold} 件`,money(s.today_proceeds_minor)+" 到账"],["当前在售",`${s.active} 件`,"已核实正式挂单"],["调价处理中",`${s.repricing} 件`,"撤单 / 返库 / 重新上架"],["需要关注",`${s.pending_confirmation+s.attention} 件`,`${s.pending_confirmation} 件待确认 · ${s.attention} 件待核实`]].map(([a,b,c])=>`<article><span class="muted">${a}</span><strong>${b}</strong><small class="muted">${c}</small></article>`).join("");
      let rows, head, total;
      if(view === "settlements") {
        head=["成交日期","饰品","购入成本","实际到账","钱包收益","记录来源","操作"];
        rows=d.settlements.map(saleRow); total=d.settlements_total;
      } else if(view === "listings") {
        head=["饰品","状态","当前挂价","预计到账","挂售时长","下次评估 / 最近判断","操作"];
        total=d.listings.length;
        rows=d.listings.slice((page-1)*50,page*50).map(r=>{const l=r.listing || {};return `<tr>${itemCell(r)}<td><span class="trading-badge ${esc(r.state)}">${labels[r.state] || esc(r.state)}</span></td><td>${money(l.buyer_minor,l.currency)}</td><td>${money(l.seller_minor,l.currency)}</td><td>${age(l.activated_at)}<small class="muted">${l.activation_source === "steam_created" ? "按 Steam 挂单时间" : "从首次核实在售计时"}</small></td><td>${evaluationLabel(r,d.policy)}</td><td>${detailButton(r)}${r.job?.status === "needs_review" ? `<button class="btn btn-sm btn-secondary" data-trading-verify="${esc(r.job.id)}">重新核实</button>` : ""}</td></tr>`;});
      } else {
        head=["购买时间","饰品","购入成本","当前状态","挂单 / 到账","操作"];
        total=d.total;
        rows=d.items.map(r=>`<tr><td>${date(r.purchased_at)}</td>${itemCell(r)}<td>${money(r.cost_minor)}</td><td><span class="trading-badge ${esc(r.state)}">${labels[r.state] || esc(r.state)}</span></td><td>${r.settlement ? money(r.settlement.proceeds_minor,r.settlement.currency)+" 到账" : r.listing ? money(r.listing.buyer_minor,r.listing.currency)+" 挂价" : "—"}<small class="muted">${r.listing_count} 次上架记录</small></td><td>${detailButton(r)}</td></tr>`);
      }
      el("trading-head").innerHTML = "<tr>"+head.map(x=>`<th>${x}</th>`).join("")+"</tr>";
      el("trading-body").innerHTML = rows.join("") || `<tr><td colspan="${head.length}" class="trading-empty">当前条件下没有记录</td></tr>`;
      el("trading-filter-note").textContent = view === "listings" ? "展示全部当前挂单，不受购买日期筛选影响。价格为最近一次 Steam 核实结果。" : `${view === "settlements" ? "按成交日期筛选，日期未知的旧记录仅在“全部”中显示" : "按实际购买时间筛选"} · 北京时间自然日`;
      el("trading-status").disabled = view !== "purchases";
      el("trading-days").disabled = view === "listings";
      el("trading-page").textContent = `第 ${page} / ${Math.max(1,Math.ceil(total/50))} 页 · ${total} 条`;
      el("trading-prev").disabled = page <= 1;
      el("trading-next").disabled = page*50 >= total;
      if(document.querySelector("#panel-sales.active")) {
        const all=await fetchJson(API+"/trading?page_size=50");
        el("trading-sales-body").innerHTML=all.settlements.map(saleRow).join("") || '<tr><td colspan="7" class="trading-empty">暂无成交记录</td></tr>';
      }
    } catch(e) { toast("交易记录加载失败", e.message || "请求失败"); }
  };
  for (const id of ["trading-view","trading-days","trading-status"]) el(id)?.addEventListener("change",()=>{page=1;refreshTrading();});
  let searchTimer;
  el("trading-query")?.addEventListener("input",()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>{page=1;refreshTrading();},300);});
  el("trading-prev")?.addEventListener("click",()=>{page=Math.max(1,page-1);refreshTrading();});
  el("trading-next")?.addEventListener("click",()=>{page++;refreshTrading();});
  el("trading-sync")?.addEventListener("click",async()=>{try {const r=await fetchJson(API+"/trading/reconcile",{method:"POST"});toast(r.message || "已安排核实");refreshTrading();}catch(e){toast("核实失败",e.message);}});
  el("trading-policy-form")?.addEventListener("submit",async e=>{
    e.preventDefault();if(busy)return;busy=true;
    try {const r=await fetchJson(API+"/trading/policy",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({enabled:el("trading-enabled").checked,age_hours:Number(el("trading-age").value),max_loss_percent:Number(el("trading-loss").value),min_change_percent:Number(el("trading-delta-pct").value),min_change_minor:Math.round(Number(el("trading-delta-amount").value)*100)})});if(r.ok){hydrated=false;toast("调价规则已保存");refreshTrading();}}catch(e){toast("保存失败",e.message);}finally{busy=false;}
  });
  el("trading-detail-close")?.addEventListener("click",()=>el("trading-detail").close());
  document.addEventListener("click",async e=>{
    const verify=e.target.closest("[data-trading-verify]");
    if(verify){try{const r=await fetchJson(API+"/trading/jobs/"+encodeURIComponent(verify.dataset.tradingVerify)+"/verify",{method:"POST"});toast(r.ok?"已安排重新核实":r.error);refreshTrading();}catch(err){toast("核实失败",err.message);}return;}
    const button=e.target.closest("[data-trading-detail]");if(!button)return;
    try {
      const d=await fetchJson(API+"/trading/items/"+button.dataset.tradingDetail);
      el("trading-detail-title").textContent=d.item.name;
      el("trading-detail-subtitle").textContent=`物品 #${d.item.id} · 北京时间 · “记录时间”代表系统发现或保存的时间`;
      el("trading-timeline").innerHTML=d.events.map(ev=>{
        const dt=ev.details || {}, parts=[];
        const eventLabels = {reprice_planned:"调价：等待重试", reprice_removing:"调价：准备撤单，核实库存", reprice_awaiting_inventory:"调价：撤单结果待核实"};
        let message = eventLabels[ev.kind] || ev.message;
        if(ev.kind === "reprice_awaiting_inventory" && (dt.reason || "").includes("尚未提交")) message = "撤单前检查失败（未提交撤单）";
        if(ev.kind === "reprice_awaiting_inventory" && (dt.reason || "").includes("下架请求失败 HTTP 429")) message = "撤单请求被 Steam 限流";
        if(dt.cost_minor!=null)parts.push("购入成本 "+money(dt.cost_minor));
        if(dt.buyer_minor!=null)parts.push("挂价 "+money(dt.buyer_minor));
        if(dt.old_buyer_minor!=null)parts.push(`${ev.kind === "reprice_completed" ? "挂价已调整" : "调价目标"}：${money(dt.old_buyer_minor)} → ${money(dt.new_buyer_minor ?? dt.target_buyer_minor)}`);
        if(dt.proceeds_minor!=null)parts.push("实际到账 "+money(dt.proceeds_minor,dt.currency));
        if(dt.date_label)parts.push(dt.date_label+"（Steam 日期）");
        if(dt.reason && dt.reason !== message)parts.push(dt.reason);
        if(dt.request_sent === false)parts.push("本次未向 Steam 提交撤单请求");
        if(dt.retry_at)parts.push("下次重试不早于 "+date(dt.retry_at));
        if(dt.consecutive_failures)parts.push("连续失败 "+dt.consecutive_failures+" 次");
        return `<article class="trading-event"><small class="muted">${ev.occurred_at ? date(ev.occurred_at) : "记录时间："+date(ev.recorded_at)}</small><h3>${esc(message)}</h3>${parts.map(p=>`<p>${esc(p)}</p>`).join("")}${ev.source==="legacy"?'<small class="muted">历史迁移记录；缺失信息未作推测</small>':""}</article>`;
      }).join("") || '<p class="muted">暂无事件</p>';
      el("trading-detail").showModal();
    } catch(err){toast("时间线加载失败",err.message);}
  });
  setInterval(()=>{if(!document.hidden && document.querySelector("#panel-purchase-history.active, #panel-sales.active"))refreshTrading();},15000);
})();
