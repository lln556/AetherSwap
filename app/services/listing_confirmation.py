"""Reconcile owned listing states and confirm only their exact Steam listing IDs."""

from app.config_loader import get_steam_credentials
from app.steam_confirm import auto_confirm_once
from app.steam_listings import fetch_listing_snapshot
from app.trading_ledger import record_snapshot


def sync_listing_confirmations(ctx, cfg, cred, assetids=None, snapshot=None):
    selected = None if assetids is None else {str(aid) for aid in assetids}
    purchases = [p for p in ctx.state.get_purchases()
                 if p.get("listing") and p.get("assetid") and not p.get("sold_at")
                 and not p.get("sale_price") and not p.get("pending_receipt")
                 and (selected is None or str(p["assetid"]) in selected)]
    if not purchases:
        return True, snapshot or {}, ""

    def same_account():
        current = get_steam_credentials()
        return (str(current.get("steam_id") or "") == str(cred.get("steam_id") or "")
                and current.get("cookies") == cred.get("cookies"))

    if not same_account():
        return False, {}, "Steam 账号或会话已变化，跳过确认"
    if snapshot is None:
        ok, snapshot, err = fetch_listing_snapshot(cred.get("cookies", ""))
        if not ok:
            ctx.log(f"[确认] 无法核实上架状态：{err}，稍后重试", "warn", category="steam")
            return False, {}, err

    def update_states(rows):
        # Re-read before each write so a concurrent sale/delist is not reverted.
        current = {p.get("_db_id"): p for p in ctx.state.get_purchases() if p.get("_db_id")}
        eligible_ids = set()
        changed = []
        for p in purchases:
            aid = str(p["assetid"])
            row = rows.get(aid)
            now = current.get(p.get("_db_id"))
            if (not row or not now or not now.get("listing") or now.get("sale_price")
                    or now.get("sold_at") or str(now.get("assetid")) != aid):
                continue
            eligible_ids.add(p["_db_id"])
            if now.get("listing_status") != row["status"]:
                changed.append((aid, row["status"]))
        # One transaction updates purchases, listing identities, activation
        # time and journal. Background reconciliation uses this same entry.
        record_snapshot(rows, str(cred.get("steam_id") or ""), purchase_ids=eligible_ids)
        for aid, status in changed:
            label = "已正式在售" if status == "active" else "等待 Steam 确认"
            ctx.log(f"[确认] assetid={aid} {label}", "info", category="steam")

    if not same_account():
        return False, {}, "Steam 账号或会话已变化，跳过确认"
    update_states(snapshot)
    eligible = {str(p["assetid"]) for p in ctx.state.get_purchases()
                if p.get("listing") and p.get("assetid") and not p.get("sale_price")
                and not p.get("sold_at") and not p.get("pending_receipt")}
    targets = {snapshot[str(p["assetid"])]["listing_id"] for p in purchases
               if str(p["assetid"]) in snapshot and str(p["assetid"]) in eligible
               and snapshot[str(p["assetid"])]["status"] == "pending_confirmation"}
    if not targets:
        return True, snapshot, ""
    confirm_cfg = cfg.get("steam_confirm") or {}
    if not confirm_cfg.get("enabled"):
        ctx.log(f"[确认] {len(targets)} 笔上架待确认，请在 Steam App 确认", "info", category="steam")
        return True, snapshot, ""
    secret = (confirm_cfg.get("identity_secret") or "").strip()
    if not secret:
        ctx.log("[确认] 缺少 identity_secret，请在 Steam App 确认上架", "warn", category="steam")
        return True, snapshot, ""
    if not same_account():
        return False, {}, "Steam 账号或会话已变化，跳过确认"
    ok, count, err = auto_confirm_once(
        secret, confirm_cfg.get("device_id") or "", str(cred.get("steam_id") or ""),
        cred.get("cookies", ""), listing_ids=targets,
    )
    if not ok:
        ctx.log(f"[确认] 自动确认未完成：{err}；保留待确认状态，稍后重试", "warn", category="steam")
        return True, snapshot, ""
    if count:
        # Confirmation success is not proof that the listing is already active.
        ok, refreshed, err = fetch_listing_snapshot(cred.get("cookies", ""))
        if ok and same_account():
            snapshot = refreshed
            update_states(snapshot)
        else:
            ctx.log("[确认] 确认请求已接受，正式在售状态待下次核实", "info", category="steam")
    if not same_account():
        return False, {}, "Steam 账号或会话已变化，跳过后续核对"
    return True, snapshot, ""
