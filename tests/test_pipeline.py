from datetime import UTC, datetime

import pytest

from homehunt import pipeline
from homehunt.analyze import AnalyzeError, Verdict
from homehunt.config import Settings
from homehunt.models import Listing
from homehunt.score import Score
from homehunt.sources.zillow import Batch, Card
from homehunt.store import Store

NOW = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)


def verdict(kind="worth_seeing", urgent=False, fit=80, value=80):
    return Verdict(
        verdict=kind, fit=fit, value=value, scam_risk="low", scam_signs=[],
        laundry="in_unit", parking="garage", utilities_paid_by_tenant=[],
        summary="s", reasons_for=[], risks=[], monthly_cost_note="$0",
        questions_for_landlord=[], needs_clarification_urgent=urgent, facts_unknown=[],
        highlights=[],
    )


def listing(sid, price=3000, address=None):
    address = address or f"{sid} Oak St"
    return Listing(
        source="zillow", source_id=sid, url=f"https://zillow/{sid}", title=address,
        price=price, beds=2, address=address, city="Pittsburgh", zipcode="15219",
        laundry=["In Unit"], has_garage=False,
    )


def card(sid, price=3000):
    return Card(source_id=sid, url=f"https://zillow/{sid}", price=price)


class FakeFetcher:
    """Each poll consumes one scripted batch; detailed listings are only
    returned for ids the pipeline said it already knows nothing about."""

    def __init__(self):
        self.script = []
        self.known_seen = []

    def push(self, cards=(), listings=(), blocked=False):
        self.script.append(Batch(list(cards), list(listings), blocked))


class FakeNotifier:
    def __init__(self):
        self.cards = []
        self.texts = []

    def send_card(self, lst, v, detected, reason, old_price=None, score=None):
        self.cards.append((lst.source_id, lst.price, reason, old_price))
        self.scores = getattr(self, "scores", []) + [score]
        return len(self.cards)

    def send_text(self, text):
        self.texts.append(text)
        return 1


class FakeAnalyzer:
    def __init__(self, store):
        self.store = store
        self.verdicts = {}  # source_id -> Verdict
        self.fail = set()  # source_ids that raise
        self.calls = []

    def __call__(self, lst, reason):
        self.calls.append((lst.source_id, reason))
        lid = self.store.db.execute(
            "SELECT id FROM listings WHERE source_id=?", (lst.source_id,)
        ).fetchone()["id"]
        if lst.source_id in self.fail:
            self.store.set_status(lid, "pending_analysis")
            raise AnalyzeError("boom")
        self.store.set_status(lid, "analyzed")
        return self.verdicts.get(lst.source_id, verdict("skip"))


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = Store(tmp_path / "t.db")
    fetcher = FakeFetcher()

    def fake_fetch(f, settings, known, sleep):
        f.known_seen.append(set(known))
        return f.script.pop(0)

    monkeypatch.setattr(pipeline, "fetch_new", fake_fetch)
    e = type("Env", (), {})()
    e.totals, e.fails = {}, {}  # source_id -> total / hard_fail reason

    def fake_score(lst, settings, places):
        return Score(
            total=e.totals.get(lst.source_id, 70),
            full_monthly_cost=lst.price + 300,
            criteria={"cost": (50.0, 50), "safety": (None, 12)},
            place="Squirrel Hill",
            hard_fail=e.fails.get(lst.source_id),
        )

    monkeypatch.setattr(pipeline, "compute_score", fake_score)
    e.store, e.fetcher = store, fetcher
    e.analyzer, e.notifier = FakeAnalyzer(store), FakeNotifier()
    e.settings = Settings()
    e.poll = lambda: pipeline.poll(
        e.settings, store, fetcher, e.analyzer, e.notifier,
        now=lambda: NOW, sleep=lambda s: None,
    )
    return e


def first_run(env):
    env.analyzer.verdicts["1"] = verdict("worth_seeing")
    env.fetcher.push(
        [card(s) for s in "123"], [listing(s) for s in "123"]
    )
    return env.poll()


def test_new_cards_one_notified_two_silent(env):
    s = first_run(env)
    assert (s.cards, s.new, s.analyzed, s.notified) == (3, 3, 3, 1)
    assert [c[0] for c in env.notifier.cards] == ["1"]
    assert env.store.pending_analysis() == []


@pytest.mark.parametrize(
    ("v", "sent"),
    [
        (verdict("worth_seeing", fit=80, value=80), True),
        (verdict("worth_seeing", fit=70, value=70), True),
        (verdict("worth_seeing", fit=80, value=60), False),
        (verdict("worth_seeing", fit=60, value=90), False),
        (verdict("doubtful", fit=90, value=90), False),
        (verdict("skip", urgent=True), False),
        (verdict("worth_seeing", urgent=True, fit=60, value=60), False),
        (verdict("worth_seeing", urgent=True, fit=80, value=80), True),
    ],
)
def test_new_listing_notified_only_when_worth_seeing_and_scores_high(env, v, sent):
    env.analyzer.verdicts["1"] = v
    env.fetcher.push([card("1")], [listing("1")])
    env.poll()
    assert [c[0] for c in env.notifier.cards] == (["1"] if sent else [])


@pytest.mark.parametrize(("rel", "val", "sent"), [(75, 75, True), (75, 50, False)])
def test_price_drop_reanalysis_applies_same_thresholds(env, rel, val, sent):
    first_run(env)
    env.analyzer.verdicts["1"] = verdict("worth_seeing", fit=rel, value=val)
    env.fetcher.push([card("1", 2600)])
    env.poll()
    assert (len(env.notifier.cards) == 2) is sent


def test_thresholds_come_from_settings(env):
    from dataclasses import replace

    env.settings = replace(
        env.settings,
        notify=replace(env.settings.notify, min_reliability=50, min_value=50),
    )
    env.analyzer.verdicts["1"] = verdict("worth_seeing", fit=60, value=60)
    env.fetcher.push([card("1")], [listing("1")])
    env.poll()
    assert [c[0] for c in env.notifier.cards] == ["1"]


def stored_scores(env):
    return env.store.db.execute("SELECT * FROM scores ORDER BY id").fetchall()


def test_hard_fail_listing_burns_no_claude_call_and_is_not_notified(env):
    env.fails["1"] = "rent 3000 > max 1500"
    env.analyzer.verdicts["1"] = verdict("worth_seeing")
    env.fetcher.push([card("1")], [listing("1")])
    env.poll()
    assert env.analyzer.calls == []
    assert env.notifier.cards == []
    assert env.store.pending_analysis() == []
    (row,) = stored_scores(env)
    assert row["hard_fail"] == "rent 3000 > max 1500"
    assert row["full_cost"] == 3300 and row["place"] == "Squirrel Hill"
    assert env.store.latest_analysis(row["listing_id"])["verdict"] == "filtered"


def test_hard_fail_on_price_drop_skips_claude_and_saves_price(env):
    first_run(env)
    env.analyzer.calls.clear()
    env.fails["1"] = "no in-unit laundry"
    env.fetcher.push([card("1", 2600)])
    env.poll()
    assert env.analyzer.calls == []
    assert len(env.notifier.cards) == 1
    assert env.store.db.execute("SELECT price FROM listings WHERE source_id='1'").fetchone()["price"] == 2600


@pytest.mark.parametrize(
    ("total", "v", "sent"),
    [
        (70, verdict("worth_seeing"), True),
        (55, verdict("worth_seeing"), True),
        (40, verdict("worth_seeing"), False),
        (70, verdict("worth_seeing").model_copy(update={"scam_risk": "high"}), False),
        (70, verdict("worth_seeing").model_copy(update={"scam_risk": "medium"}), True),
        (70, verdict("worth_seeing").model_copy(update={"laundry": "shared"}), False),
        (70, verdict("worth_seeing").model_copy(update={"laundry": "coin"}), False),
        (70, verdict("worth_seeing").model_copy(update={"laundry": "hookups"}), False),
    ],
)
def test_notify_rule_needs_score_no_high_scam_and_in_unit_laundry(env, total, v, sent):
    env.totals["1"] = total
    env.analyzer.verdicts["1"] = v
    env.fetcher.push([card("1")], [listing("1")])
    env.poll()
    assert [c[0] for c in env.notifier.cards] == (["1"] if sent else [])
    assert len(stored_scores(env)) == 1
    if sent:
        assert env.notifier.scores[0].total == total


def test_min_score_comes_from_settings(env):
    from dataclasses import replace

    env.settings = replace(env.settings, notify=replace(env.settings.notify, min_score=30))
    env.totals["1"] = 40
    env.analyzer.verdicts["1"] = verdict("worth_seeing")
    env.fetcher.push([card("1")], [listing("1")])
    env.poll()
    assert [c[0] for c in env.notifier.cards] == ["1"]


def test_same_cards_next_run_no_analysis_no_alert(env):
    first_run(env)
    env.analyzer.calls.clear()
    env.fetcher.push([card(s) for s in "123"])
    s = env.poll()
    assert env.analyzer.calls == []
    assert len(env.notifier.cards) == 1
    assert (s.new, s.notified) == (0, 0)
    assert env.fetcher.known_seen[1] == {"1", "2", "3"}


def test_price_drop_reanalyzes_and_alerts_again(env):
    first_run(env)
    env.fetcher.push([card("1", 2600), card("2"), card("3")])
    env.poll()
    assert env.analyzer.calls[-1] == ("1", "price_drop")
    assert env.notifier.cards[-1] == ("1", 2600, "price_drop", 3000)
    row = env.store.db.execute("SELECT price FROM listings WHERE source_id='1'").fetchone()
    assert row["price"] == 2600


def test_small_drop_does_nothing(env):
    first_run(env)
    env.analyzer.calls.clear()
    env.fetcher.push([card("1", 2950), card("2"), card("3")])
    env.poll()
    assert env.analyzer.calls == []
    assert len(env.notifier.cards) == 1


def test_price_drop_to_skip_verdict_is_not_notified(env):
    first_run(env)
    env.analyzer.verdicts["1"] = verdict("skip")
    env.fetcher.push([card("1", 2600)])
    env.poll()
    assert len(env.notifier.cards) == 1


def test_failed_analysis_stays_pending_then_succeeds(env):
    env.analyzer.fail.add("1")
    env.analyzer.verdicts["1"] = verdict("worth_seeing")
    env.fetcher.push([card("1")], [listing("1")])
    s = env.poll()
    assert s.errors == 1 and env.notifier.cards == []
    assert env.store.pending_analysis() != []

    env.analyzer.fail.clear()
    env.fetcher.push([card("1")])
    env.poll()
    assert env.store.pending_analysis() == []
    assert [c[0] for c in env.notifier.cards] == ["1"]
    assert env.fetcher.known_seen[1] == {"1"}  # pending is not re-fetched


def test_pending_capped_by_max_details_per_run(env):
    from dataclasses import replace

    env.settings = replace(env.settings, poll=replace(env.settings.poll, max_details_per_run=2))
    env.fetcher.push([card(s) for s in "123"], [listing(s) for s in "123"])
    env.poll()
    assert [c[0] for c in env.analyzer.calls] == ["1", "2"]
    env.fetcher.push([card(s) for s in "123"])
    env.poll()
    assert env.analyzer.calls[-1] == ("3", "new")


def test_failed_price_drop_reanalysis_is_retried(env):
    first_run(env)
    env.analyzer.fail.add("1")
    env.fetcher.push([card("1", 2600)])
    env.poll()
    assert len(env.notifier.cards) == 1
    env.analyzer.fail.clear()
    env.fetcher.push([card("1", 2600)])
    env.poll()
    assert env.notifier.cards[-1] == ("1", 2600, "price_drop", 3000)


def test_blocked_runs_alert_exactly_once_per_streak(env):
    def blocked():
        env.fetcher.push(blocked=True)
        return env.poll()

    blocked()
    assert env.notifier.texts == []
    s = blocked()
    assert s.blocked and len(env.notifier.texts) == 1
    assert "2 запусков" in env.notifier.texts[0]
    blocked()
    assert len(env.notifier.texts) == 1

    env.fetcher.push([card("1")], [listing("1")])
    env.poll()  # healthy
    blocked()
    blocked()
    assert len(env.notifier.texts) == 2
    runs = env.store.recent_runs(1)
    assert runs[0]["note"] == "blocked" and runs[0]["errors"] == 1


def test_zero_card_runs_count_as_bad(env):
    env.fetcher.push()
    env.poll()
    env.fetcher.push()
    env.poll()
    assert len(env.notifier.texts) == 1


def test_repost_same_price_is_silent(env):
    first_run(env)
    env.analyzer.calls.clear()
    env.fetcher.push([card("1"), card("9")], [listing("9", address="1 Oak St")])
    s = env.poll()
    assert env.analyzer.calls == []
    assert len(env.notifier.cards) == 1
    assert s.new == 1
    assert env.store.pending_analysis() == []


def test_repost_with_price_drop_is_analyzed_and_alerted(env):
    first_run(env)
    env.analyzer.verdicts["9"] = verdict("worth_seeing")
    env.fetcher.push([card("1"), card("9", 2600)], [listing("9", price=2600, address="1 Oak St")])
    env.poll()
    assert env.analyzer.calls[-1] == ("9", "price_drop")
    assert env.notifier.cards[-1] == ("9", 2600, "price_drop", 3000)
    assert env.store.pending_analysis() == []
