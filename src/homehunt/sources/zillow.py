"""Zillow rentals source: search page and item page, read from embedded JSON.

The search page carries ``<script id="__NEXT_DATA__">``; the item page carries
the same script with a JSON *string* ``gdpClientCache`` inside. The visual
markup is never parsed.
"""

from __future__ import annotations

import json
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import quote, urljoin

import httpx

from homehunt.config import Settings
from homehunt.models import Listing

BASE = "https://www.zillow.com"
_NEXT_RE = re.compile(
    r"<script[^>]*id=\"__NEXT_DATA__\"[^>]*>(.*?)</script>", re.DOTALL
)
_MAX_PHOTOS = 20


class Blocked(Exception):
    """Zillow served a bot-check page instead of the listing data."""


class PageShapeError(Exception):
    """The page has no expected payload (Zillow changed its shape)."""


@dataclass
class Card:
    source_id: str
    url: str
    price: int
    beds: int | None = None
    baths: float | None = None
    address: str | None = None
    city: str | None = None
    zipcode: str | None = None
    lat: float | None = None
    lon: float | None = None
    photo: str | None = None
    home_type: str | None = None


# ------------------------------------------------------------------ search url


def search_url(settings: Settings) -> str:
    s = settings.search
    state = {
        "pagination": {},
        "isMapVisible": True,
        "mapBounds": {
            "west": s.bounds["west"],
            "east": s.bounds["east"],
            "south": s.bounds["south"],
            "north": s.bounds["north"],
        },
        "filterState": {
            "sort": {"value": "days"},
            "fr": {"value": True},
            "fsba": {"value": False},
            "fsbo": {"value": False},
            "nc": {"value": False},
            "cmsn": {"value": False},
            "auc": {"value": False},
            "fore": {"value": False},
            "mp": {"max": s.max_rent},
            "beds": {"min": s.min_beds},
            "apa": {"value": False},
            "con": {"value": False},
            "apco": {"value": False},
        },
    }
    if s.in_unit_laundry:
        state["filterState"]["lau"] = {"value": True}
    payload = json.dumps(state, separators=(",", ":"))
    return f"{BASE}/homes/for_rent/?searchQueryState={quote(payload, safe='')}"


# --------------------------------------------------------------------- helpers


def _next_data(html: str) -> dict:
    if "px-captcha" in html:
        raise Blocked("px-captcha")
    m = _NEXT_RE.search(html)
    if not m:
        raise PageShapeError("no __NEXT_DATA__ script")
    try:
        data = json.loads(m.group(1))
    except ValueError as exc:
        raise PageShapeError("__NEXT_DATA__ is not JSON") from exc
    if not isinstance(data, dict):
        raise PageShapeError("__NEXT_DATA__ is not an object")
    return data


def _get(d: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(d, dict):
            return None
        d = d.get(key)
    return d


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _price_text(text: Any) -> int | None:
    m = re.search(r"\d[\d,]*", text) if isinstance(text, str) else None
    return int(m.group().replace(",", "")) if m else None


def _str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str) and v]
    return []


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


# ---------------------------------------------------------------- search page


def parse_search(html: str) -> list[Card]:
    data = _next_data(html)
    results = _get(data, "props", "pageProps", "searchPageState", "cat1",
                   "searchResults", "listResults")
    if not isinstance(results, list):
        raise PageShapeError("no searchResults.listResults")
    cards: dict[str, Card] = {}
    for r in results:
        if not isinstance(r, dict):
            continue
        zpid = str(r.get("zpid") or "")
        if not zpid.isdigit() or zpid in cards:
            continue  # apartment buildings carry "lat--long" ids
        price = _int(r.get("unformattedPrice"))
        if price is None:
            price = _price_text(r.get("price"))
        if price is None:
            continue
        url = r.get("detailUrl")
        if not isinstance(url, str) or not url:
            continue
        info = _get(r, "hdpData", "homeInfo") or {}
        cards[zpid] = Card(
            source_id=zpid,
            url=urljoin(BASE + "/", url),
            price=price,
            beds=_int(r.get("beds")),
            baths=_float(r.get("baths")),
            address=r.get("address") if isinstance(r.get("address"), str) else None,
            city=r.get("addressCity") if isinstance(r.get("addressCity"), str) else None,
            zipcode=r.get("addressZipcode")
            if isinstance(r.get("addressZipcode"), str) else None,
            lat=_float(_get(r, "latLong", "latitude")),
            lon=_float(_get(r, "latLong", "longitude")),
            photo=r.get("imgSrc") if isinstance(r.get("imgSrc"), str) else None,
            home_type=info.get("homeType") if isinstance(info, dict) else None,
        )
    return list(cards.values())


# ------------------------------------------------------------------ item page


def _photos(prop: dict) -> list[str]:
    out: list[str] = []
    entries = prop.get("responsivePhotos")
    for entry in entries if isinstance(entries, list) else []:
        jpegs = _get(entry, "mixedSources", "jpeg")
        best: str | None = None
        if isinstance(jpegs, list):
            sized = [j for j in jpegs if isinstance(j, dict) and isinstance(j.get("url"), str)]
            if sized:
                best = max(sized, key=lambda j: _int(j.get("width")) or 0)["url"]
        if best is None and isinstance(entry, dict) and isinstance(entry.get("url"), str):
            best = entry["url"]
        if best and best not in out:
            out.append(best)
        if len(out) >= _MAX_PHOTOS:
            break
    return out


def _date_ms(value: Any) -> date | None:
    ms = _int(value)
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=UTC).date()
    except (OverflowError, OSError, ValueError):
        return None


def parse_item(html: str) -> dict[str, Any]:
    """Rental fields of the item page, keyed by ``Listing`` field names."""
    data = _next_data(html)
    cache = _get(data, "props", "pageProps", "componentProps", "gdpClientCache")
    if isinstance(cache, str):
        try:
            cache = json.loads(cache)
        except ValueError as exc:
            raise PageShapeError("gdpClientCache is not JSON") from exc
    if not isinstance(cache, dict) or not cache:
        raise PageShapeError("no gdpClientCache")
    prop = _get(next(iter(cache.values())), "property")
    if not isinstance(prop, dict):
        raise PageShapeError("no property in gdpClientCache")
    reso = prop.get("resoFacts")
    reso = reso if isinstance(reso, dict) else {}

    baths = _float(prop.get("bathrooms"))
    full = _int(prop.get("bathroomsFull"))
    half = _int(prop.get("bathroomsHalf"))
    if full is None and baths is not None:
        full = int(baths)
    return {
        "price": _int(prop.get("price")),
        "beds": _int(prop.get("bedrooms")),
        "baths_full": full,
        "baths_half": half or 0,
        "area_sqft": next(
            (a for a in (_int(prop.get(k)) for k in ("livingArea", "livingAreaValue"))
             if a is not None),
            _int(reso.get("livingArea")),
        ),
        "home_type": prop.get("homeType"),
        "address": prop.get("streetAddress"),
        "city": prop.get("city"),
        "zipcode": prop.get("zipcode"),
        "lat": _float(prop.get("latitude")),
        "lon": _float(prop.get("longitude")),
        "days_on_zillow": _int(prop.get("daysOnZillow")),
        "description": prop.get("description"),
        "laundry": _str_list(reso.get("laundryFeatures")),
        "parking": _str_list(reso.get("parkingFeatures")),
        "has_garage": _bool(reso.get("hasGarage")),
        "garage_capacity": _int(reso.get("garageParkingCapacity")),
        "cooling": _str_list(reso.get("cooling")),
        "heating": _str_list(reso.get("heating")),
        "appliances": _str_list(reso.get("appliances")),
        "pets_allowed": _bool(reso.get("hasPetsAllowed")),
        "deposit": _int(reso.get("securityDeposit")),
        "available_from": _date_ms(reso.get("availabilityDate")),
        "photos": _photos(prop),
    }


def to_listing(card: Card, item: dict[str, Any]) -> Listing:
    """Join a search card with its item-page fields into a ``Listing``."""
    address = item.get("address") or card.address
    price = item.get("price") or card.price
    return Listing(
        source="zillow",
        source_id=card.source_id,
        url=card.url,
        title=address or card.source_id,
        price=price,
        beds=item.get("beds") if item.get("beds") is not None else card.beds,
        baths_full=item.get("baths_full"),
        baths_half=item.get("baths_half"),
        area_sqft=item.get("area_sqft"),
        home_type=item.get("home_type") or card.home_type,
        address=address,
        city=item.get("city") or card.city,
        zipcode=item.get("zipcode") or card.zipcode,
        lat=item.get("lat") if item.get("lat") is not None else card.lat,
        lon=item.get("lon") if item.get("lon") is not None else card.lon,
        description=item.get("description"),
        photos=item.get("photos") or ([card.photo] if card.photo else []),
        laundry=item.get("laundry") or [],
        parking=item.get("parking") or [],
        has_garage=item.get("has_garage"),
        cooling=item.get("cooling") or [],
        pets_allowed=item.get("pets_allowed"),
        deposit=item.get("deposit"),
        available_from=item.get("available_from"),
        days_on_zillow=item.get("days_on_zillow"),
        raw={
            k: item[k]
            for k in ("heating", "appliances", "garage_capacity")
            if item.get(k) not in (None, [], "")
        },
    )


# -------------------------------------------------------------------- fetching

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
        "image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Linux"',
}


@dataclass
class FetchResult:
    status: int
    final_url: str
    text: str


class Fetcher:
    def __init__(self, client: httpx.Client | None = None) -> None:
        self.client = client or httpx.Client(
            headers=_HEADERS, timeout=30.0, follow_redirects=True
        )

    def get(self, url: str) -> FetchResult:
        resp = self.client.get(url)
        return FetchResult(
            status=resp.status_code, final_url=str(resp.url), text=resp.text
        )


@dataclass
class Batch:
    cards: list[Card] = field(default_factory=list)
    listings: list[Listing] = field(default_factory=list)
    blocked: bool = False
    errors: list[str] = field(default_factory=list)


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
    try:
        batch.cards = parse_search(res.text)
    except Blocked:
        batch.blocked = True
        return batch
    except PageShapeError as exc:
        batch.errors.append(f"search: {exc}")
        return batch

    cards = [c for c in batch.cards if c.source_id not in known_ids][
        : settings.poll.max_details_per_run
    ]  # the search is sorted newest first (sort=days)
    lo, hi = settings.poll.detail_delay_s
    for i, card in enumerate(cards):
        if i:
            sleep(rng.uniform(lo, hi))
        try:
            page = fetcher.get(card.url)
            batch.listings.append(to_listing(card, parse_item(page.text)))
        except Blocked:
            batch.blocked = True
            break
        except Exception as exc:  # noqa: BLE001 - one bad item must not lose the batch
            batch.errors.append(f"{card.source_id}: {exc!r}")
    return batch
