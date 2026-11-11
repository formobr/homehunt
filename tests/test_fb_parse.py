import json
import random
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from homehunt.config import Settings
from homehunt.sources import fb

FIXTURES = Path(__file__).parent / "fixtures"
HOUSE_ID = "1650785769512096"
TOWNHOUSE_ID = "27923477983923497"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def page(*payloads) -> str:
    return "<html>" + "".join(
        f'<script type="application/json" nonce="n" data-sjs>{json.dumps(p)}</script>'
        for p in payloads
    ) + "</html>"


def story(lid, title, price="1200.00", **extra):
    return {
        "id": lid,
        "marketplace_listing_title": title,
        "listing_price": {"amount": price},
        "location": {"reverse_geocode": {"city": "Pittsburgh", "state": "PA"}},
        "primary_listing_photo": {"image": {"uri": f"https://img/{lid}.jpg"}},
        "is_sold": False,
        "is_pending": False,
        **extra,
    }


def feed(*stories):
    return {"marketplace_feed_stories": {"edges": [{"node": {"listing": s}} for s in stories]}}


# ------------------------------------------------------------------- search url


def test_search_url_comes_from_settings():
    s = Settings()
    assert fb.search_url(s) == (
        "https://www.facebook.com/marketplace/pittsburgh/propertyrentals"
        "?maxPrice=1500&minBedrooms=2&sortBy=creation_time_descend&exact=false"
    )
    other = replace(s, search=replace(s.search, fb_city="austin", max_rent=1900, min_beds=3))
    assert fb.search_url(other) == (
        "https://www.facebook.com/marketplace/austin/propertyrentals"
        "?maxPrice=1900&minBedrooms=3&sortBy=creation_time_descend&exact=false"
    )


# ------------------------------------------------------------------ search page


def test_search_fixture_keeps_only_houses_and_townhouses():
    cards, blocked = fb.parse_search(fixture("fb_rentals_search.html"))
    assert not blocked
    assert len(cards) == 11  # 24 cards on the page; the rest are apartments
    titles = [c.title.lower() for c in cards]
    assert all("house" in t for t in titles)  # "townhouse" contains it
    assert not any("apartment" in t or "departamento" in t for t in titles)
    assert len({c.source_id for c in cards}) == len(cards)


def test_search_fixture_card_fields():
    cards, _ = fb.parse_search(fixture("fb_rentals_search.html"))
    card = next(c for c in cards if c.source_id == HOUSE_ID)
    assert card.title == "4 Beds 1 Bath - House"
    assert card.url == f"https://www.facebook.com/marketplace/item/{HOUSE_ID}/"
    assert card.price == 1495
    assert (card.city, card.state) == ("Pittsburgh", "PA")
    assert card.photo and card.photo.startswith("https://")
    town = next(c for c in cards if c.source_id == TOWNHOUSE_ID)
    assert (town.title, town.price) == ("2 Beds 1 Bath Townhouse", 1300)


def test_search_drops_sold_pending_and_non_house_cards():
    html = page(
        feed(
            story("1", "3 Beds 1 Bath - House"),
            story("2", "2 Beds 1 Bath - Apartment"),
            story("3", "Room for rent in a house share", price="500.00", is_sold=True),
            story("4", "2 Beds 1 Bath Townhouse", is_pending=True),
            story("5", "1 Bed 1 Bath - Condo"),
            story("6", "Private Room"),
            story("7", "2 Beds 1 Bath Townhouse"),
            story("8", "3 Beds - House", price=None),
        )
    )
    cards, blocked = fb.parse_search(html)
    assert not blocked
    assert [c.source_id for c in cards] == ["1", "7"]


def test_search_merges_dicts_of_one_card_and_keeps_page_order():
    partial = {"id": "1", "marketplace_listing_title": "3 Beds - House"}
    rest = {"id": "1", "listing_price": {"amount": "1100.00"},
            "location": {"reverse_geocode": {"city": "Verona", "state": "PA"}}}
    cards, _ = fb.parse_search(page(feed(partial), {"x": [rest]}, feed(story("0", "House"))))
    assert [c.source_id for c in cards] == ["1", "0"]
    assert (cards[0].price, cards[0].city) == (1100, "Verona")


def test_blocked_is_no_cards_and_no_feed_payload():
    assert fb.parse_search("<html>Log in to Facebook</html>") == ([], True)
    assert fb.parse_search(page({"require": []}))[1] is True
    # an empty but real feed is a quiet day, not a block
    assert fb.parse_search(page(feed())) == ([], False)
    # a feed with only apartments is not a block either
    cards, blocked = fb.parse_search(page(feed(story("1", "2 Beds - Apartment"))))
    assert (cards, blocked) == ([], False)


def test_search_ignores_script_that_is_not_json():
    html = page(feed(story("1", "House"))).replace(
        "</html>", '<script type="application/json">{broken</script></html>'
    )
    assert [c.source_id for c in fb.parse_search(html)[0]] == ["1"]


# -------------------------------------------------------------------- item page


def test_item_fixture_house():
    item = fb.parse_item(fixture("fb_rental_item.html"), HOUSE_ID)
    assert item["home_type"] == "SINGLE_FAMILY"
    assert (item["beds"], item["baths_full"], item["baths_half"]) == (4, 1, 0)
    assert item["price"] == 1495
    assert item["cooling"] == ["Air Conditioner"]
    assert item["laundry"] == ["In Unit"]
    assert item["parking"] == ["On Street"]
    assert item["pets_allowed"] is True
    assert item.get("has_garage") is None
    assert item["address"] == "5515 2nd Ave"
    assert item["city"] == "Pittsburgh"
    assert item["zipcode"] == "15207"
    assert (item["lat"], item["lon"]) == (40.40151, -79.93973)
    assert item["description"].startswith("4 Beds 1 Bath - House")
    assert 0 < len(item["photos"]) <= 20
    assert all(p.startswith("https://") for p in item["photos"])


def test_item_fixture_townhouse():
    item = fb.parse_item(fixture("fb_rental_item_townhouse.html"), TOWNHOUSE_ID)
    assert item["home_type"] == "TOWNHOUSE"
    assert (item["beds"], item["baths_full"], item["baths_half"]) == (2, 1, 0)
    assert item["laundry"] == []  # "Laundry available" is not in-unit
    assert item["parking"] == ["Off Street"]
    assert item["address"] == "237 Penn St"
    assert (item["city"], item["zipcode"]) == ("Verona", "15147")
    assert (item["lat"], item["lon"]) == (40.49877, -79.84622)


def item_page(*fields, **extra):
    item = {
        "id": "9",
        "marketplace_listing_title": "T",
        "pdp_display_sections": [
            {"section_type": "UNIT_INCLUDES",
             "pdp_fields": [{"display_label": label, "icon_name": icon} for icon, label in fields]}
        ],
        **extra,
    }
    return page({"data": {"target": item}})


@pytest.mark.parametrize(
    ("icon", "label", "key", "value"),
    [
        ("house", "House", "home_type", "SINGLE_FAMILY"),
        ("house", "Townhouse", "home_type", "TOWNHOUSE"),
        ("house", "Apartment", "home_type", None),
        ("washing-machine", "In-unit laundry", "laundry", ["In Unit"]),
        ("washing-machine", "Laundry available", "laundry", []),
        ("washing-machine", "Laundry in building", "laundry", []),
        ("car", "Garage parking", "parking", ["Garage"]),
        ("car", "Off-street parking", "parking", ["Off Street"]),
        ("car", "Street parking", "parking", ["On Street"]),
        ("weather-cold", "AC available", "cooling", ["Air Conditioner"]),
        ("paw", "Dog and cat friendly", "pets_allowed", True),
        ("sqft", "1,250 sq ft", "area_sqft", 1250),
    ],
)
def test_item_field_mapping(icon, label, key, value):
    assert fb.parse_item(item_page((icon, label)), "9")[key] == value


def test_item_garage_sets_has_garage():
    assert fb.parse_item(item_page(("car", "Garage parking")), "9")["has_garage"] is True


def test_item_beds_and_baths_with_a_half_bath():
    item = fb.parse_item(item_page(("bedrooms-bathrooms", "3 beds · 1.5 baths")), "9")
    assert (item["beds"], item["baths_full"], item["baths_half"]) == (3, 1, 1)
    item = fb.parse_item(item_page(("bedrooms-bathrooms", "2 beds · 2 baths")), "9")
    assert (item["beds"], item["baths_full"], item["baths_half"]) == (2, 2, 0)


def test_item_published_at_from_creation_time():
    item = fb.parse_item(item_page(("house", "House"), creation_time=1794279203), "9")
    assert item["published_at"].isoformat() == "2026-11-10T02:53:23+00:00"


def test_item_photos_are_capped_at_twenty_and_deduped():
    photos = [{"image": {"uri": f"https://img/{i % 25}.jpg"}} for i in range(60)]
    item = fb.parse_item(item_page(("house", "House"), listing_photos=photos), "9")
    assert len(item["photos"]) == 20
    assert len(set(item["photos"])) == 20


def test_item_without_payload_for_the_id_is_unavailable():
    with pytest.raises(fb.ItemUnavailable):
        fb.parse_item(fixture("fb_rental_item.html"), "123456")
    with pytest.raises(fb.ItemUnavailable):
        fb.parse_item("<html>This content isn't available</html>", HOUSE_ID)
    # payloads that merely mention the id are not the item
    with pytest.raises(fb.ItemUnavailable):
        fb.parse_item(page({"seoPagePath": "x", "id": "9"}), "9")


def test_to_listing_joins_card_and_item():
    card = fb.parse_search(fixture("fb_rentals_search.html"))[0]
    card = next(c for c in card if c.source_id == HOUSE_ID)
    lst = fb.to_listing(card, fb.parse_item(fixture("fb_rental_item.html"), HOUSE_ID))
    assert lst.source == "fb" and lst.source_id == HOUSE_ID
    assert lst.url == card.url
    assert lst.title == "5515 2nd Ave"
    assert (lst.price, lst.beds, lst.home_type) == (1495, 4, "SINGLE_FAMILY")
    assert lst.laundry == ["In Unit"] and lst.parking == ["On Street"]
    assert lst.fingerprint == "5515 2nd ave|15207"


def test_to_listing_without_address_titles_by_listing_title_and_has_unique_fingerprint():
    card = fb.Card("9", "u", "3 Beds - House", 1000, photo="https://img/c.jpg")
    lst = fb.to_listing(card, fb.parse_item(item_page(("house", "House")), "9"))
    assert lst.title == "T"
    assert lst.photos == ["https://img/c.jpg"]
    assert lst.fingerprint == "fb:9"


# --------------------------------------------------------------------- fetching


class FakeFetcher:
    def __init__(self, pages, status=200):
        self.pages, self.status, self.urls = pages, status, []

    def get(self, url):
        self.urls.append(url)
        if isinstance(self.pages, Exception):
            raise self.pages
        return SimpleNamespace(status=self.status, final_url=url, text=self.pages.get(url, "<html></html>"))


def fetch(fetcher, known=(), settings=None):
    sleeps = []
    batch = fb.fetch_new(
        fetcher, settings or Settings(), set(known), sleeps.append, random.Random(1)
    )
    return batch, sleeps


def pages():
    return {
        fb.search_url(Settings()): fixture("fb_rentals_search.html"),
        fb.item_url(HOUSE_ID): fixture("fb_rental_item.html"),
        fb.item_url(TOWNHOUSE_ID): fixture("fb_rental_item_townhouse.html"),
    }


def test_fetch_new_returns_listings_and_skips_unavailable_items_quietly():
    f = FakeFetcher(pages())
    batch, sleeps = fetch(f)
    assert not batch.blocked and batch.errors == []
    assert len(batch.cards) == 11
    assert sorted(item.source_id for item in batch.listings) == sorted([HOUSE_ID, TOWNHOUSE_ID])
    assert all(item.source == "fb" for item in batch.listings)
    assert len(sleeps) == 10  # between the 11 item fetches, not before the first
    assert all(2 <= s <= 5 for s in sleeps)


def test_fetch_new_skips_known_ids_and_caps_details():
    f = FakeFetcher(pages())
    batch, _ = fetch(f, known={HOUSE_ID})
    assert fb.item_url(HOUSE_ID) not in f.urls
    assert len(f.urls) == 1 + 10
    capped = FakeFetcher(pages())
    settings = Settings()
    settings = replace(settings, poll=replace(settings.poll, max_details_per_run=3))
    batch, _ = fetch(capped, settings=settings)
    assert len(capped.urls) == 1 + 3
    assert len(batch.cards) == 11  # price-drop checks still see every card


@pytest.mark.parametrize("status", [403, 429])
def test_fetch_new_blocked_by_status(status):
    batch, _ = fetch(FakeFetcher(pages(), status=status))
    assert batch.blocked and batch.listings == []


@pytest.mark.parametrize("status", [403, 429])
def test_fetch_new_item_page_block_is_a_block_not_a_removed_item(status):
    class ItemsBlocked(FakeFetcher):
        def get(self, url):
            res = super().get(url)
            if "/marketplace/item/" in url:
                res.status = status
            return res

    f = ItemsBlocked(pages())
    batch, _ = fetch(f)
    assert batch.blocked and batch.listings == []
    assert len(f.urls) == 2  # the search, then the first item; the rest are not hammered


def test_fetch_new_blocked_by_login_wall():
    batch, _ = fetch(FakeFetcher({fb.search_url(Settings()): "<html>Log in</html>"}))
    assert batch.blocked and batch.cards == []


def test_fetch_new_network_error_is_an_error_not_a_block():
    batch, _ = fetch(FakeFetcher(httpx.ConnectError("down")))
    assert not batch.blocked and len(batch.errors) == 1
