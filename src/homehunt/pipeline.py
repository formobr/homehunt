"""One poll run end to end: fetch, dedup, re-rate on price drop, analyze, notify."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from .analyze import AnalyzeError, Verdict
from .config import Settings
from .models import Listing
from .notify import NotifyError
from .score import Score, load_places
from .score import score as compute_score
from .sources.zillow import fetch_new
from .store import Store

log = logging.getLogger(__name__)

SOURCE = "zillow"
HEALTH_ALERT = "homehunt: Zillow не отдаёт объявления {n} запусков подряд"


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class RunSummary:
    run_id: int
    cards: int = 0
    new: int = 0
    analyzed: int = 0
    notified: int = 0
    errors: int = 0
    blocked: bool = False
    alerted: bool = False


def _dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def _load(store: Store, lid: int) -> tuple[Listing, datetime, int | None]:
    r = store.db.execute("SELECT * FROM listings WHERE id=?", (lid,)).fetchone()
    listing = Listing(
        source=r["source"],
        source_id=r["source_id"],
        url=r["url"],
        title=r["title"] or "",
        price=r["price"] or 0,
        beds=r["beds"],
        baths_full=r["baths_full"],
        baths_half=r["baths_half"],
        area_sqft=r["area_sqft"],
        home_type=r["home_type"],
        address=r["address"],
        city=r["city"],
        zipcode=r["zipcode"],
        lat=r["lat"],
        lon=r["lon"],
        description=r["description"],
        photos=json.loads(r["photos"]) if r["photos"] else [],
        laundry=json.loads(r["laundry"]) if r["laundry"] else [],
        parking=json.loads(r["parking"]) if r["parking"] else [],
        has_garage=None if r["has_garage"] is None else bool(r["has_garage"]),
        cooling=json.loads(r["cooling"]) if r["cooling"] else [],
        pets_allowed=None if r["pets_allowed"] is None else bool(r["pets_allowed"]),
        deposit=r["deposit"],
        available_from=date.fromisoformat(r["available_from"]) if r["available_from"] else None,
        days_on_zillow=r["days_on_zillow"],
        published_at=_dt(r["published_at"]),
        raw=json.loads(r["raw"]) if r["raw"] else {},
    )
    return listing, _dt(r["detected_at"]) or utcnow(), r["repost_of"]


def _price_of(store: Store, lid: int) -> int | None:
    r = store.db.execute("SELECT price FROM listings WHERE id=?", (lid,)).fetchone()
    return r["price"] if r else None


def _is_drop(old: int | None, new: int, settings: Settings) -> bool:
    if not old or new >= old:
        return False
    drop = old - new
    return (
        drop >= settings.notify.price_drop_min_usd
        or drop / old * 100 >= settings.notify.price_drop_min_pct
    )


def _qualifies(v: Verdict, settings: Settings) -> bool:
    return (
        v.verdict == "worth_seeing"
        and v.fit >= settings.notify.min_reliability
        and v.value >= settings.notify.min_value
    )


def _notify_ok(v: Verdict, s: Score, settings: Settings) -> bool:
    return (
        _qualifies(v, settings)
        and v.scam_risk != "high"
        and v.laundry not in ("shared", "coin", "hookups")
        and s.total >= settings.notify.min_score
    )


def poll(
    settings: Settings,
    store: Store,
    fetcher: Any,
    analyzer: Callable[[Listing, str], Verdict],
    notifier: Any,
    now: Callable[[], datetime] = utcnow,
    sleep: Callable[[float], None] = time.sleep,
) -> RunSummary:
    """``analyzer(listing, reason) -> Verdict`` must mark the listing analyzed
    on success and pending_analysis on AnalyzeError (as analyze.analyze does)."""
    run_id = store.start_run(SOURCE)
    summary = RunSummary(run_id)
    known = {
        r["source_id"]
        for r in store.db.execute(
            "SELECT source_id FROM listings WHERE source=?", (SOURCE,)
        )
    }
    batch = fetch_new(fetcher, settings, known, sleep)
    summary.cards = len(batch.cards)
    summary.errors = len(batch.errors)

    if batch.blocked:
        summary.blocked = True
        summary.errors += 1
        store.finish_run(run_id, summary.cards, 0, summary.errors, "blocked")
        _health(settings, store, notifier, summary)
        return summary

    skip: set[int] = set()  # failed this run; retried next run

    places = load_places()

    def rate(lid: int, listing: Listing, reason: str) -> tuple[Score, bool]:
        """Score before the AI; a hard fail is recorded and means no claude call."""
        sc = compute_score(listing, settings, places)
        store.record_score(
            lid, sc.total, sc.full_monthly_cost, sc.place,
            {k: {"value": v, "weight": w} for k, (v, w) in sc.criteria.items()},
            sc.hard_fail,
        )
        if sc.hard_fail:
            store.record_analysis(
                lid, "filter", None, "filtered", None, None,
                {"hard_fail": sc.hard_fail}, None, reason,
            )
            store.set_status(lid, "analyzed")
        return sc, sc.hard_fail is not None

    def notify(lid: int, listing: Listing, verdict: Verdict, detected: datetime,
               reason: str, sc: Score, old_price: int | None = None) -> None:
        if not _notify_ok(verdict, sc, settings):
            return
        if reason != "price_drop" and store.was_notified(lid):
            return
        try:
            mid = notifier.send_card(listing, verdict, detected, reason, old_price, score=sc)
        except NotifyError as exc:
            log.warning("notify failed for %s: %s", lid, exc)
            summary.errors += 1
            return
        store.record_notification(lid, mid)
        summary.notified += 1

    # 1. price drops on known listings
    for card in batch.cards:
        if card.source_id not in known:
            continue
        row = store.db.execute(
            "SELECT id, price, status FROM listings WHERE source=? AND source_id=?",
            (SOURCE, card.source_id),
        ).fetchone()
        if row is None or row["status"] != "analyzed":
            continue
        if not _is_drop(row["price"], card.price, settings):
            continue
        lid = row["id"]
        listing, detected, _ = _load(store, lid)
        old = listing.price
        listing.price = card.price
        sc, failed = rate(lid, listing, "price_drop")
        if failed:
            store.upsert_listing(listing, now())  # save the price: the drop is handled
            continue
        try:
            verdict = analyzer(listing, "price_drop")
        except AnalyzeError as exc:
            log.warning("re-analysis failed for %s: %s", lid, exc)
            store.set_status(lid, "analyzed")  # price not saved: drop is seen again
            summary.errors += 1
            continue
        store.upsert_listing(listing, now())
        summary.analyzed += 1
        notify(lid, listing, verdict, detected, "price_drop", sc, old)

    # 2. new detailed listings
    for item in batch.listings:
        res = store.upsert_listing(item, now())
        if not res.is_new:
            continue
        summary.new += 1
        if res.repost_of is None:
            continue
        old = _price_of(store, res.repost_of)
        if not _is_drop(old, item.price, settings):
            store.set_status(res.listing_id, "analyzed")
            continue
        sc, failed = rate(res.listing_id, item, "price_drop")
        if failed:
            continue
        try:
            verdict = analyzer(item, "price_drop")
        except AnalyzeError as exc:
            log.warning("repost analysis failed for %s: %s", res.listing_id, exc)
            summary.errors += 1
            skip.add(res.listing_id)
            continue
        summary.analyzed += 1
        _, detected, _ = _load(store, res.listing_id)
        notify(res.listing_id, item, verdict, detected, "price_drop", sc, old)

    # 3. pending analyses, oldest first, capped
    todo = [i for i in store.pending_analysis() if i not in skip]
    for lid in todo[: settings.poll.max_details_per_run]:
        listing, detected, _ = _load(store, lid)
        sc, failed = rate(lid, listing, "new")
        if failed:
            continue
        try:
            verdict = analyzer(listing, "new")
        except AnalyzeError as exc:
            log.warning("analysis failed for %s: %s", lid, exc)
            summary.errors += 1
            continue
        summary.analyzed += 1
        notify(lid, listing, verdict, detected, "new", sc)

    store.finish_run(run_id, summary.cards, summary.new, summary.errors, None)
    _health(settings, store, notifier, summary)
    return summary


def _bad(run: dict) -> bool:
    return run.get("note") == "blocked" or run.get("cards") == 0


def _health(settings: Settings, store: Store, notifier: Any, summary: RunSummary) -> None:
    n = settings.health.fail_runs_before_alert
    runs = store.recent_runs(n + 1)
    if len(runs) < n or not all(_bad(r) for r in runs[:n]):
        return
    if len(runs) > n and _bad(runs[n]):
        return
    try:
        notifier.send_text(HEALTH_ALERT.format(n=n))
        summary.alerted = True
    except NotifyError as exc:
        log.warning("health alert failed: %s", exc)
