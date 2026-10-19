import json
from app.services.trading_timeline import listing_timeline
from app.trading_models import MarketListing, TradeEvent


def event(kind, at, details):
    return TradeEvent(purchase_id=1, kind=kind, message='已核实正式在售',
                      occurred_at=at, recorded_at=at, details=json.dumps(details))


def listing(lid, start, end=None):
    return MarketListing(purchase_id=1, assetid='a', steam_listing_id=lid,
                         submitted_at=start, ended_at=end)


def test_legacy_double_observation_shows_first_time_and_preserves_raw_events():
    events = [event('listing_state', 200, {'listing_status': {'old': 'pending_confirmation', 'new': 'active'}}),
              event('listing_verified', 500, {'listing_id': 'lid', 'state': 'active'})]
    result = listing_timeline(events, [listing('lid', 100)])
    assert len(result) == 1 and result[0]['occurred_at'] == 200
    assert result[0]['details']['merged_event_ids'] == [e.id for e in events]
    assert len(events) == 2
    assert 'merged_event_ids' not in events[0].details


def test_real_relisting_retains_separate_milestones():
    events = [event('listing_verified', 200, {'listing_id': 'old', 'state': 'active'}),
              event('listing_state', 400, {'listing_status': {'new': 'active'}}),
              event('listing_verified', 500, {'listing_id': 'new', 'state': 'active'})]
    result = listing_timeline(events, [listing('old', 100, 300), listing('new', 350)])
    assert [e['occurred_at'] for e in result] == [200, 400]
    assert [e['details']['listing_id'] for e in result] == ['old', 'new']


def test_unmatched_and_multi_field_changes_are_not_hidden():
    events = [event('listing_state', 50, {'listing_status': {'new': 'active'}}),
              event('listing_state', 200, {'listing_status': {'new': 'active'}, 'price': {'old': 1, 'new': 2}}),
              event('listing_verified', 500, {'listing_id': 'lid', 'state': 'active'})]
    assert len(listing_timeline(events, [listing('lid', 100)])) == 3


def test_repeated_observation_of_same_listing_is_one_milestone():
    events = [event('listing_verified', at, {'listing_id': 'lid', 'state': 'active'}) for at in (200, 300)]
    result = listing_timeline(events, [listing('lid', 100)])
    assert len(result) == 1 and result[0]['occurred_at'] == 200
