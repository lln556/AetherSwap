"""Present one milestone per listing, retaining the original audit events."""
import json


def listing_timeline(events, listings):
    rows = [{**e.model_dump(), "details": json.loads(e.details)} for e in events]
    verified = {
        (e["details"].get("listing_id"), e["details"].get("state"))
        for e in rows if e["kind"] == "listing_verified"
    }
    # Legacy listing_state events have no listing ID. Associate them only if
    # exactly one known listing spans that time, and matching evidence exists.
    known = sorted((l for l in listings if l.steam_listing_id), key=lambda l: l.submitted_at)
    windows = []
    for i, listing in enumerate(known):
        end = listing.ended_at if listing.ended_at is not None else float('inf')
        if i + 1 < len(known):
            end = min(end, known[i + 1].submitted_at)
        windows.append((listing.submitted_at, end, listing.steam_listing_id))
    result, milestones = [], {}
    for event in rows:
        details = event["details"]
        key = None
        if event["kind"] == "listing_verified":
            key = (details.get("listing_id"), details.get("state"))
        elif event["kind"] == "listing_state" and set(details) <= {"listing", "listing_status"}:
            state = (details.get("listing_status") or {}).get("new")
            at = event["occurred_at"] if event["occurred_at"] is not None else event["recorded_at"]
            matches = [lid for start, end, lid in windows if start <= at < end]
            if len(matches) == 1 and (matches[0], state) in verified:
                key = (matches[0], state)
        if key and key[0] and key[1] in {"active", "pending_confirmation"}:
            if key in milestones:
                first = milestones[key]
                first["details"].setdefault("merged_event_ids", [first["id"]]).append(event["id"])
                continue
            milestones[key] = event
            event["details"] = {**details, "listing_id": key[0], "state": key[1]}
        result.append(event)
    return result
