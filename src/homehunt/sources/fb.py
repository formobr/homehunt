"""Facebook Marketplace rentals source, logged out.

Everything is read from the page's ``<script type="application/json">``
payloads: walk every dict, and for an item merge the dicts whose ``id`` equals
the item id. The visual markup is never parsed.
"""

from __future__ import annotations

import json
import random
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from homehunt.config import Settings
from homehunt.models import Listing
from homehunt.sources.zillow import Batch
from homehunt.sources.zillow import Fetcher as _ChromeFetcher

BASE = "https://www.facebook.com"
_SCRIPT_RE = re.compile(
    r"<script[^>]*type=\"application/json\"[^>]*>(.*?)</script>", re.DOTALL
)
_HOUSE_RE = re.compile(r"\b(?:town)?house\b", re.IGNORECASE)
_BEDS_BATHS_RE = re.compile(r"(\d+)\s*beds?\b.*?(\d+(?:\.\d+)?)\s*baths?\b", re.IGNORECASE)
_SQFT_RE = re.compile(r"([\d,]+)\s*sq\.?\s*ft\.?\s*$", re.IGNORECASE)
_MAX_PHOTOS = 20


class ItemUnavailable(Exception):
    """The item page carries no payload for the item (removed or hidden)."""


@dataclass
class Card:
    source_id: str
    url: str
    title: str
    price: int
    city: str | None = None
    state: str | None = None
    photo: str | None = None


# ------------------------------------------------------------------ search url


def search_url(settings: Settings) -> str:
    s = settings.search
    return (
        f"{BASE}/marketplace/{s.fb_city}/propertyrentals"
        f"?maxPrice={s.max_rent}&minBedrooms={s.min_beds}"
        "&sortBy=creation_time_descend&exact=false"
    )


def item_url(item_id: str) -> str:
    return f"{BASE}/marketplace/item/{item_id}/"


# --------------------------------------------------------------------- helpers


def _payloads(html: str) -> Iterator[Any]:
    for m in _SCRIPT_RE.finditer(html):
        try:
            yield json.loads(m.group(1))
        except ValueError:
            continue


def _dicts(node: Any) -> Iterator[dict]:
    """Every dict in the tree in document order, iteratively (payloads nest deeply)."""
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            yield cur
            stack.extend(reversed(list(cur.values())))
        elif isinstance(cur, list):
            stack.extend(reversed(cur))


def _merge(into: dict, extra: dict) -> None:
    """Fill keys ``into`` lacks (or holds empty); nested dicts merge the same way."""
    for k, v in extra.items():
        cur = into.get(k)
        if k not in into or cur in (None, "", [], {}):
            into[k] = v
        elif isinstance(cur, dict) and isinstance(v, dict):
            _merge(cur, v)


def _get(d: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(d, dict):
            return None
        d = d.get(key)
    return d


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _price(d: dict) -> int | None:
    amount = _num(_get(d, "listing_price", "amount"))
    return int(amount) if amount is not None else None


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


# ---------------------------------------------------------------- search page


def parse_search(html: str) -> tuple[list[Card], bool]:
    """(cards, blocked). No cards and no feed payload means FB served something else."""
    by_id: list[tuple[str, dict]] = []
    titled: list[str] = []  # card ids in page order
    feed = False
    for payload in _payloads(html):
        for d in _dicts(payload):
            stories = d.get("marketplace_feed_stories")
            if isinstance(stories, dict) and "edges" in stories:
                feed = True
            lid = d.get("id")
            if isinstance(lid, str) and lid.isdigit():
                by_id.append((lid, d))
                if "marketplace_listing_title" in d and lid not in titled:
                    titled.append(lid)
    merged: dict[str, dict] = {lid: {} for lid in titled}
    for lid, d in by_id:
        if lid in merged:
            _merge(merged[lid], d)
    cards: list[Card] = []
    for lid, d in merged.items():
        if d.get("is_sold") or d.get("is_pending"):
            continue
        title = _str(d.get("marketplace_listing_title"))
        price = _price(d)
        if not title or price is None or not _HOUSE_RE.search(title):
            continue  # apartments, rooms, condos: the plan wants house or townhouse
        cards.append(
            Card(
                source_id=lid,
                url=item_url(lid),
                title=title,
                price=price,
                city=_str(_get(d, "location", "reverse_geocode", "city")),
                state=_str(_get(d, "location", "reverse_geocode", "state")),
                photo=_str(_get(d, "primary_listing_photo", "image", "uri")),
            )
        )
    blocked = not cards and not feed
    return cards, blocked


# ------------------------------------------------------------------ item page


def _fields(item: dict) -> Iterator[tuple[str, str]]:
    sections = item.get("pdp_display_sections")
    for section in sections if isinstance(sections, list) else []:
        fields = section.get("pdp_fields") if isinstance(section, dict) else None
        for f in fields if isinstance(fields, list) else []:
            if isinstance(f, dict):
                icon, label = f.get("icon_name"), f.get("display_label")
                if isinstance(icon, str) and isinstance(label, str):
                    yield icon, label.strip()


def _photos(item: dict) -> list[str]:
    out: list[str] = []
    photos = item.get("listing_photos")
    for p in photos if isinstance(photos, list) else []:
        uri = _str(_get(p, "image", "uri"))
        if uri and uri not in out:
            out.append(uri)
        if len(out) >= _MAX_PHOTOS:
            break
    return out


def parse_item(html: str, item_id: str) -> dict[str, Any]:
    """Rental fields of the item page, keyed by ``Listing`` field names."""
    item: dict = {}
    for payload in _payloads(html):
        for d in _dicts(payload):
            if d.get("id") == item_id:
                _merge(item, d)
    if not item or not (
        item.get("pdp_display_sections") or item.get("marketplace_listing_title")
    ):
        raise ItemUnavailable(item_id)

    out: dict[str, Any] = {
        "price": _price(item),
        "laundry": [],
        "parking": [],
        "cooling": [],
    }
    for icon, label in _fields(item):
        low = label.lower()
        if icon == "house":
            out["home_type"] = {"house": "SINGLE_FAMILY", "townhouse": "TOWNHOUSE"}.get(low)
        elif icon == "bedrooms-bathrooms":
            m = _BEDS_BATHS_RE.search(label)
            if m:
                out["beds"] = int(m.group(1))
                baths = float(m.group(2))
                out["baths_full"] = int(baths)
                out["baths_half"] = 1 if baths % 1 else 0
        elif icon == "washing-machine":
            # only "in-unit" is a known good; the AI reads the description otherwise
            out["laundry"] = ["In Unit"] if low == "in-unit laundry" else []
        elif icon == "car":
            if low == "garage parking":
                out["parking"], out["has_garage"] = ["Garage"], True
            elif low == "off-street parking":
                out["parking"] = ["Off Street"]
            elif low == "street parking":
                out["parking"] = ["On Street"]
        elif icon == "weather-cold":
            out["cooling"] = ["Air Conditioner"]
        elif icon == "paw":
            out["pets_allowed"] = True
        else:
            m = _SQFT_RE.search(label)
            if m:
                out["area_sqft"] = int(m.group(1).replace(",", ""))

    postal = _str(_get(item, "location", "reverse_geocode_detailed", "postal_code"))
    created = _num(item.get("creation_time"))
    published = None
    if created is not None:
        try:
            published = datetime.fromtimestamp(created, tz=UTC)
        except (OverflowError, OSError, ValueError):
            published = None
    out.update(
        address=_str(_get(item, "home_address", "street")),
        city=_str(_get(item, "location", "reverse_geocode_detailed", "city")),
        zipcode=postal[:5] if postal else None,
        lat=_num(_get(item, "location", "latitude")),
        lon=_num(_get(item, "location", "longitude")),
        description=_str(_get(item, "redacted_description", "text")),
        photos=_photos(item),
        published_at=published,
        title=_str(item.get("marketplace_listing_title")),
    )
    return out


def to_listing(card: Card, item: dict[str, Any]) -> Listing:
    """Join a search card with its item-page fields into a ``Listing``."""
    address = item.get("address")
    return Listing(
        source="fb",
        source_id=card.source_id,
        url=card.url,
        title=address or item.get("title") or card.title,
        price=item.get("price") or card.price,
        beds=item.get("beds"),
        baths_full=item.get("baths_full"),
        baths_half=item.get("baths_half"),
        area_sqft=item.get("area_sqft"),
        home_type=item.get("home_type"),
        address=address,
        city=item.get("city") or card.city,
        zipcode=item.get("zipcode"),
        lat=item.get("lat"),
        lon=item.get("lon"),
        description=item.get("description"),
        photos=item.get("photos") or ([card.photo] if card.photo else []),
        laundry=item.get("laundry") or [],
        parking=item.get("parking") or [],
        has_garage=item.get("has_garage"),
        cooling=item.get("cooling") or [],
        pets_allowed=item.get("pets_allowed"),
        published_at=item.get("published_at"),
    )


# -------------------------------------------------------------------- fetching


class Fetcher(_ChromeFetcher):
    """Plain HTTP client with Chrome headers, as for Zillow."""


def fetch_new(
    fetcher: Fetcher,
    settings: Settings,
    known_ids: set[str],
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> Batch:
    rng = rng or random.Random()
    batch = Batch()
    try:
        res = fetcher.get(search_url(settings))
    except httpx.HTTPError as exc:
        batch.errors.append(f"search: {exc!r}")
        return batch
    if res.status in (403, 429):
        batch.blocked = True
        return batch
    batch.cards, blocked = parse_search(res.text)
    if blocked:
        batch.blocked = True
        return batch

    cards = [c for c in batch.cards if c.source_id not in known_ids][
        : settings.poll.max_details_per_run
    ]  # the search is sorted newest first
    lo, hi = settings.poll.detail_delay_s
    for i, card in enumerate(cards):
        if i:
            sleep(rng.uniform(lo, hi))
        try:
            page = fetcher.get(card.url)
            if page.status in (403, 429):
                batch.blocked = True
                break
            batch.listings.append(to_listing(card, parse_item(page.text, card.source_id)))
        except ItemUnavailable:
            continue
        except Exception as exc:  # noqa: BLE001 - one bad item must not lose the batch
            batch.errors.append(f"{card.source_id}: {exc!r}")
    return batch
