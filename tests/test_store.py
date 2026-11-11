import os
from datetime import UTC, date, datetime

import pytest

from homehunt.config import load_env, load_settings
from homehunt.models import Listing
from homehunt.store import Store

T0 = datetime(2026, 1, 1, tzinfo=UTC)
T1 = datetime(2026, 1, 2, tzinfo=UTC)


def mk(source_id="a1", price=5000, **kw):
    base = {
        "source": "zillow",
        "source_id": source_id,
        "url": "http://x/" + source_id,
        "title": "300 Miltenberger St",
        "price": price,
        "beds": 3,
        "address": "300 Miltenberger St",
        "city": "Pittsburgh",
        "zipcode": "15219",
        "laundry": ["In Unit"],
        "has_garage": False,
        "available_from": date(2027, 5, 1),
    }
    base.update(kw)
    return Listing(**base)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "sub" / "t.db")


def test_new_then_known_listing_is_not_duplicated(store):
    r1 = store.upsert_listing(mk(), T0)
    r2 = store.upsert_listing(mk(), T1)
    assert r1.is_new and not r2.is_new
    assert r1.listing_id == r2.listing_id
    assert r2.old_price is None
    n = store.db.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    assert n == 1
    assert store.db.execute("SELECT COUNT(*) FROM price_history").fetchone()[0] == 1


def test_price_change_recorded_with_old_price(store):
    r1 = store.upsert_listing(mk(price=1400), T0)
    r2 = store.upsert_listing(mk(price=1300), T1)
    assert r2.old_price == 1400
    prices = [
        r[0] for r in store.db.execute(
            "SELECT price FROM price_history WHERE listing_id=? ORDER BY id",
            (r1.listing_id,),
        )
    ]
    assert prices == [1400, 1300]
    assert store.similar_prices(3, "15219") == [1300]
    assert store.similar_prices(2, "15219") == []


def test_listing_fields_round_trip_through_the_journal(store):
    store.upsert_listing(mk(), T0)
    r = store.db.execute("SELECT * FROM listings").fetchone()
    assert (r["beds"], r["zipcode"], r["has_garage"], r["pets_allowed"]) == (3, "15219", 0, None)
    assert r["laundry"] == '["In Unit"]' and r["available_from"] == "2027-05-01"


def test_repost_detected_by_fingerprint(store):
    first = store.upsert_listing(mk("a1"), T0)
    repost = store.upsert_listing(mk("b2", address="300 MILTENBERGER St."), T1)
    assert repost.is_new
    assert repost.repost_of == first.listing_id
    other = store.upsert_listing(mk("c3", address="12 Other St"), T1)
    assert other.repost_of is None
    same_street_other_zip = store.upsert_listing(mk("d4", zipcode="15201"), T1)
    assert same_street_other_zip.repost_of is None


def test_pending_analysis_and_status(store):
    a = store.upsert_listing(mk("a1"), T0).listing_id
    b = store.upsert_listing(mk("b1", address="1 Other St"), T0).listing_id
    assert store.pending_analysis() == [a, b]
    store.set_status(a, "analyzed")
    assert store.pending_analysis() == [b]
    store.record_analysis(a, "quick", "m", "buy", 7, 8, {"k": 1}, 1.5, "new")
    assert store.latest_analysis(a)["json"] == {"k": 1}
    assert store.latest_analysis(b) is None


def test_was_notified(store):
    a = store.upsert_listing(mk(), T0).listing_id
    assert not store.was_notified(a)
    store.record_notification(a, 42)
    assert store.was_notified(a)


def test_runs(store):
    rid = store.start_run("fb")
    store.finish_run(rid, cards=3, new=1, errors=0, note="ok")
    runs = store.recent_runs(5)
    assert runs[0]["cards"] == 3 and runs[0]["note"] == "ok"


def test_recent_runs_filters_by_source(store):
    for source, cards in (("zillow", 1), ("fb", 2), ("zillow", 3)):
        store.finish_run(store.start_run(source), cards=cards, new=0, errors=0)
    assert [r["cards"] for r in store.recent_runs(5)] == [3, 2, 1]
    assert [r["cards"] for r in store.recent_runs(5, "zillow")] == [3, 1]
    assert [r["cards"] for r in store.recent_runs(1, "fb")] == [2]
    assert store.recent_runs(5, "other") == []


def test_fb_city_setting_defaults_and_env_override(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("search: {}\n")
    assert load_settings(p, env={}).search.fb_city == "pittsburgh"
    assert load_settings(
        p, env={"HOMEHUNT__SEARCH__FB_CITY": "austin"}
    ).search.fb_city == "austin"


def test_unknown_config_key_refused(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("search:\n  max_rentt: 1\n")
    with pytest.raises(ValueError, match="max_rentt"):
        load_settings(p)
    p.write_text("bogus: {}\n")
    with pytest.raises(ValueError, match="bogus"):
        load_settings(p)


def test_missing_config_keys_take_defaults(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("search:\n  max_rent: 1700\n")
    s = load_settings(p)
    assert s.search.max_rent == 1700
    assert s.search.min_beds == 2
    assert s.search.bounds["west"] == -80.36
    assert s.poll.detail_delay_s == (2, 5)
    assert s.health.fail_runs_before_alert == 2


def test_config_env_overrides_apply_with_coercion(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("notify:\n  min_value: 10\n")
    s = load_settings(
        p,
        env={
            "HOMEHUNT__NOTIFY__MIN_VALUE": "65",
            "HOMEHUNT__POLL__DETAIL_DELAY_S": "[1, 3]",
            "UNRELATED": "x",
        },
    )
    assert s.notify.min_value == 65
    assert s.poll.detail_delay_s == (1, 3)


def test_config_env_unknown_override_refused(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("")
    for name in (
        "HOMEHUNT__NOTIFY__BOGUS",
        "HOMEHUNT__BOGUS__X",
        "HOMEHUNT__NOTIFY",
        "HOMEHUNT__NOTIFY__MIN_VALUE__EXTRA",
    ):
        with pytest.raises(ValueError, match="unknown config key"):
            load_settings(p, env={name: "1"})


def test_env_does_not_override_existing(tmp_path, monkeypatch):
    monkeypatch.setenv("CH_TEST_SET", "orig")
    monkeypatch.delenv("CH_TEST_NEW", raising=False)
    p = tmp_path / ".env"
    p.write_text('# c\n\nCH_TEST_SET=other\nCH_TEST_NEW="val"\n')
    parsed = load_env(p)
    assert parsed == {"CH_TEST_SET": "other", "CH_TEST_NEW": "val"}
    assert os.environ["CH_TEST_SET"] == "orig"
    assert os.environ["CH_TEST_NEW"] == "val"
    monkeypatch.delenv("CH_TEST_NEW")
