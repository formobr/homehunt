import json
import random
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from homehunt.config import Settings
from homehunt.sources import zillow
from homehunt.sources.zillow import (
    Blocked,
    PageShapeError,
    fetch_new,
    parse_item,
    parse_search,
)

FIX = Path(__file__).parent / "fixtures"
SEARCH = (FIX / "zillow_search.html").read_text(encoding="utf-8")
ITEM = (FIX / "zillow_item.html").read_text(encoding="utf-8")


def page(payload: dict) -> str:
    return (
        '<html><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload)
        + "</script></html>"
    )


def search_page(results: list) -> str:
    return page({"props": {"pageProps": {"searchPageState": {"cat1": {
        "searchResults": {"listResults": results}}}}}})


def item_page(prop: dict) -> str:
    cache = json.dumps({"ForRentQuery{}": {"property": prop}})
    return page({"props": {"pageProps": {"componentProps": {"gdpClientCache": cache}}}})


def raw_card(zpid, price=1200, **kw):
    base = {"zpid": zpid, "detailUrl": f"/homedetails/x/{zpid}_zpid/", "unformattedPrice": price,
            "price": f"${price:,}/mo", "beds": 2, "baths": 1, "address": "1 A St, Pittsburgh, PA 15201",
            "addressCity": "Pittsburgh", "addressZipcode": "15201",
            "latLong": {"latitude": 40.1, "longitude": -79.9}, "imgSrc": "https://p/1.jpg"}
    base.update(kw)
    return base


# ------------------------------------------------------------------ search_url


def test_search_url_encodes_every_filter_from_settings():
    url = zillow.search_url(Settings())
    parsed = urlparse(url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "https://www.zillow.com/homes/for_rent/"
    state = json.loads(parse_qs(parsed.query)["searchQueryState"][0])
    assert state["mapBounds"] == {"west": -80.36, "east": -79.69, "south": 40.19, "north": 40.68}
    f = state["filterState"]
    assert f["lau"] == {"value": True}
    assert f["mp"] == {"max": 1500}
    assert f["beds"] == {"min": 2}
    assert f["apa"] == {"value": False} and f["con"] == {"value": False}
    assert f["apco"] == {"value": False}
    assert f["sort"] == {"value": "days"} and f["fr"] == {"value": True}
    for key in ("fsba", "fsbo", "nc", "cmsn", "auc", "fore"):
        assert f[key] == {"value": False}


def test_search_url_omits_laundry_when_not_required():
    from dataclasses import replace

    s = Settings()
    s = replace(s, search=replace(s.search, in_unit_laundry=False, max_rent=1800))
    state = json.loads(parse_qs(urlparse(zillow.search_url(s)).query)["searchQueryState"][0])
    assert "lau" not in state["filterState"]
    assert state["filterState"]["mp"] == {"max": 1800}


# ----------------------------------------------------------------- parse_search


def test_search_fixture_cards():
    cards = parse_search(SEARCH)
    assert 0 < len(cards) <= 41
    assert all(c.source_id.isdigit() for c in cards)
    assert len({c.source_id for c in cards}) == len(cards)
    assert all(c.url.startswith("https://www.zillow.com/") and c.price > 0 for c in cards)
    first = cards[0]
    assert first.source_id == "11273192"
    assert first.url == "https://www.zillow.com/homedetails/300-Miltenberger-St-Pittsburgh-PA-15219/11273192_zpid/"
    assert (first.price, first.beds, first.baths) == (1400, 3, 1)
    assert first.address == "300 Miltenberger St, Pittsburgh, PA 15219"
    assert (first.city, first.zipcode) == ("Pittsburgh", "15219")
    assert (first.lat, first.lon) == (40.437004, -79.98164)
    assert first.photo.startswith("https://photos.zillowstatic.com/")
    assert first.home_type == "SINGLE_FAMILY"


def test_building_cards_are_skipped_and_relative_urls_made_absolute():
    cards = parse_search(search_page([
        raw_card("40.1--79.9", price=900),
        raw_card("123"),
        raw_card("123"),  # duplicate
        {"zpid": "77", "detailUrl": "/x/", "price": "no digits"},  # no usable price
    ]))
    assert [c.source_id for c in cards] == ["123"]
    assert cards[0].url == "https://www.zillow.com/homedetails/x/123_zpid/"


def test_card_price_falls_back_to_price_text():
    card = raw_card("5", price=1350)
    del card["unformattedPrice"]
    assert parse_search(search_page([card]))[0].price == 1350


def test_changed_search_shape_raises_instead_of_returning_nothing():
    with pytest.raises(PageShapeError):
        parse_search("<html>no data</html>")
    with pytest.raises(PageShapeError):
        parse_search(page({"props": {"pageProps": {"searchPageState": None}}}))


def test_captcha_page_is_blocked():
    with pytest.raises(Blocked):
        parse_search('<html><div id="px-captcha"></div></html>')
    with pytest.raises(Blocked):
        parse_item('<html><div id="px-captcha"></div></html>')


# ------------------------------------------------------------------- parse_item


def test_item_fixture_fields():
    item = parse_item(ITEM)
    assert item["price"] == 1400
    assert (item["beds"], item["baths_full"], item["baths_half"]) == (3, 1, 0)
    assert item["area_sqft"] == 1224
    assert item["home_type"] == "SINGLE_FAMILY"
    assert (item["address"], item["city"], item["zipcode"]) == ("300 Miltenberger St", "Pittsburgh", "15219")
    assert item["lat"] == 40.437004
    assert item["laundry"] == ["In Unit"]
    assert item["has_garage"] is False and item["pets_allowed"] is False
    assert item["cooling"] == ["Air Conditioner", "Central Air"]
    assert item["parking"] == []
    assert item["days_on_zillow"] == 0
    assert item["available_from"] == date(2027, 5, 1)
    assert item["description"].startswith("Available (NOW) or May 2027")
    assert len(item["photos"]) == 7 == len(set(item["photos"]))
    assert all(p.startswith("https://photos.zillowstatic.com/") and p.endswith(".jpg") for p in item["photos"])


def test_item_photos_take_largest_jpeg_and_cap_at_twenty():
    photos = [
        {"mixedSources": {"jpeg": [{"url": f"https://p/{i}-s.jpg", "width": 192},
                                   {"url": f"https://p/{i}-l.jpg", "width": 1536},
                                   {"url": f"https://p/{i}-m.jpg", "width": 768}]}}
        for i in range(25)
    ]
    item = parse_item(item_page({"price": 1, "responsivePhotos": photos}))
    assert len(item["photos"]) == 20
    assert item["photos"][0] == "https://p/0-l.jpg"


def test_changed_item_shape_raises():
    with pytest.raises(PageShapeError):
        parse_item(page({"props": {"pageProps": {"componentProps": {"gdpClientCache": "{}"}}}}))
    with pytest.raises(PageShapeError):
        parse_item(page({"props": {"pageProps": {}}}))


# ------------------------------------------------------------------- fetch_new


class FakeFetcher:
    def __init__(self, pages):
        self.pages = pages
        self.urls = []

    def get(self, url):
        self.urls.append(url)
        text = self.pages(url)
        if isinstance(text, Exception):
            raise text
        return zillow.FetchResult(200, url, text)


def run_fetch(pages, known=frozenset(), settings=None):
    sleeps = []
    f = FakeFetcher(pages)
    batch = fetch_new(f, settings or Settings(), set(known), sleeps.append, random.Random(1))
    return batch, f, sleeps


def test_fetch_new_skips_known_ids_and_joins_card_with_item():
    def pages(url):
        if "searchQueryState" in url:
            return search_page([raw_card("1"), raw_card("2", price=1100), raw_card("40.1--79.9")])
        return item_page({"price": 1111, "bedrooms": 2, "streetAddress": "2 B St",
                          "zipcode": "15201", "resoFacts": {"laundryFeatures": ["In Unit"]}})

    batch, f, _ = run_fetch(pages, known={"1"})
    assert [c.source_id for c in batch.cards] == ["1", "2"]
    assert len(f.urls) == 2 and f.urls[1].endswith("/2_zpid/")
    (lst,) = batch.listings
    assert (lst.source, lst.source_id, lst.price) == ("zillow", "2", 1111)
    assert lst.address == "2 B St" and lst.laundry == ["In Unit"]
    assert lst.photos == ["https://p/1.jpg"]  # falls back to the card photo
    assert lst.fingerprint == "2 b st|15201"


def test_fetch_new_caps_details_and_sleeps_between_items():
    from dataclasses import replace

    s = Settings()
    s = replace(s, poll=replace(s.poll, max_details_per_run=2))
    def pages(url):
        if "searchQueryState" in url:
            return search_page([raw_card(str(i)) for i in range(1, 5)])
        return item_page({"price": 1})

    batch, f, sleeps = run_fetch(pages, settings=s)
    assert len(batch.listings) == 2 and len(f.urls) == 3
    assert len(sleeps) == 1 and 2 <= sleeps[0] <= 5


def test_fetch_new_blocked_search_and_blocked_item():
    batch, _, _ = run_fetch(lambda url: '<div id="px-captcha"></div>')
    assert batch.blocked and batch.cards == []

    def pages(url):
        return search_page([raw_card("1")]) if "searchQueryState" in url else "px-captcha"

    batch, _, _ = run_fetch(pages)
    assert batch.blocked and batch.listings == []


def test_fetch_new_one_bad_item_keeps_the_rest():
    def pages(url):
        if "searchQueryState" in url:
            return search_page([raw_card("1"), raw_card("2")])
        return "<html>broken</html>" if "/1_zpid/" in url else item_page({"price": 1})

    batch, _, _ = run_fetch(pages)
    assert [l.source_id for l in batch.listings] == ["2"]
    assert len(batch.errors) == 1 and batch.errors[0].startswith("1:")
