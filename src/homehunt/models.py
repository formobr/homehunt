"""Domain models."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass
class Listing:
    source: str
    source_id: str
    url: str
    title: str
    price: int  # monthly rent, USD
    beds: int | None = None
    baths_full: int | None = None
    baths_half: int | None = None
    area_sqft: int | None = None
    home_type: str | None = None
    address: str | None = None
    city: str | None = None
    zipcode: str | None = None
    lat: float | None = None
    lon: float | None = None
    description: str | None = None
    photos: list[str] = field(default_factory=list)
    laundry: list[str] = field(default_factory=list)
    parking: list[str] = field(default_factory=list)
    has_garage: bool | None = None
    cooling: list[str] = field(default_factory=list)
    pets_allowed: bool | None = None
    deposit: int | None = None
    available_from: date | None = None
    days_on_zillow: int | None = None
    published_at: datetime | None = None
    raw: dict = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        addr = re.sub(r"[^a-z0-9]+", " ", (self.address or "").lower()).strip()
        if not addr:  # never let address-less listings collapse into one "repost"
            return f"{self.source}:{self.source_id}"
        return f"{addr}|{(self.zipcode or '').strip()}"
