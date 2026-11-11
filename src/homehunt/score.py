"""Deterministic rental score (docs/PLAN.md §3 H2)."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings
from .models import Listing

RENTERS_INSURANCE = 15
UTILITY_PER_SQFT = 0.16
DEFAULT_SQFT = 1100
WATER_SEWER = 85
PLACE_RADIUS_KM = 2.5

DEFAULT_WEIGHTS = {
    "cost": 50, "safety": 12, "parking": 12, "income": 8, "type": 7, "baths": 6,
}


@dataclass
class Score:
    total: float
    full_monthly_cost: int
    criteria: dict[str, tuple[float | None, float]] = field(default_factory=dict)
    place: str | None = None
    hard_fail: str | None = None


def load_places() -> dict[str, dict]:
    path = Path(__file__).parent / "data" / "places.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["places"]


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(a))


def nearest_place(lat: float | None, lon: float | None, places: dict[str, dict]) -> str | None:
    if lat is None or lon is None:
        return None
    best, best_d = None, PLACE_RADIUS_KM
    for name, info in places.items():
        for plat, plon in info.get("points", []):
            d = _haversine_km(lat, lon, plat, plon)
            if d <= best_d:
                best, best_d = name, d
    return best


def full_monthly_cost(listing: Listing) -> int:
    text = (listing.description or "").lower()
    utilities = (listing.area_sqft or DEFAULT_SQFT) * UTILITY_PER_SQFT + WATER_SEWER
    if (
        "utilities included" in text
        or "utilities are included" in text
        or "heat and electric included" in text
        or "heat & electric included" in text
        or "heat/electric included" in text
        or "heat and electricity included" in text
    ):
        utilities = 0
    elif "water included" in text or "water is included" in text or "water and sewer included" in text:
        utilities -= WATER_SEWER
    return round(listing.price + utilities + RENTERS_INSURANCE)


def _clamp(v: float) -> float:
    return max(0.0, min(100.0, v))


def _parking(listing: Listing) -> float:
    items = [p.lower() for p in listing.parking]
    if listing.has_garage or any("garage" in p for p in items):
        return 100
    if any(k in p for p in items for k in ("driveway", "off-street", "off street", "attached", "assigned")):
        return 55
    if items and all("street" in p for p in items):
        return 0
    return 15 if not items else 0


def _type(listing: Listing) -> float:
    t = (listing.home_type or "").upper()
    if t == "SINGLE_FAMILY":
        return 100
    if t == "TOWNHOUSE":
        return 55
    return 30


def _baths(listing: Listing) -> float:
    full, half = listing.baths_full or 0, listing.baths_half or 0
    if full >= 2:
        return 100
    if full == 1 and half >= 1:
        return 80
    return 0


def _hard_fail(listing: Listing, settings: Settings) -> str | None:
    s = settings.search
    if listing.price > s.max_rent:
        return f"rent {listing.price} > max {s.max_rent}"
    if listing.beds is not None and listing.beds < s.min_beds:
        return f"beds {listing.beds} < min {s.min_beds}"
    if (
        s.in_unit_laundry
        and listing.laundry
        and not any("in unit" in x.lower() for x in listing.laundry)
    ):
        return "no in-unit laundry"
    return None


def score(listing: Listing, settings: Settings, places: dict[str, dict]) -> Score:
    cfg = settings.score
    cost = full_monthly_cost(listing)
    span = cfg.cost_worst - cfg.cost_best
    cost_v = _clamp(100 * (cfg.cost_worst - cost) / span) if span > 0 else (100.0 if cost <= cfg.cost_best else 0.0)

    place = nearest_place(listing.lat, listing.lon, places)
    info = places.get(place) if place else None
    safety_v = income_v = None
    if info:
        v, p = info.get("violent_pct"), info.get("property_pct")
        if v is not None and p is not None:
            safety_v = _clamp(round(100 - (0.6 * v + 0.4 * p)))
        income_v = info.get("income_pct")

    values = {
        "cost": cost_v,
        "safety": safety_v,
        "parking": _parking(listing),
        "income": income_v,
        "type": _type(listing),
        "baths": _baths(listing),
    }
    weights = {**DEFAULT_WEIGHTS, **cfg.weights}
    criteria = {k: (v, weights[k]) for k, v in values.items()}
    used = [(v, w) for v, w in criteria.values() if v is not None]
    wsum = sum(w for _, w in used)
    total = sum(v * w for v, w in used) / wsum if wsum else 0.0
    return Score(
        total=round(_clamp(total), 1),
        full_monthly_cost=cost,
        criteria=criteria,
        place=place,
        hard_fail=_hard_fail(listing, settings),
    )
