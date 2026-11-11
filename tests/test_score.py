from dataclasses import replace

from homehunt.config import ScoreSettings, Settings
from homehunt.models import Listing
from homehunt.score import load_places, score

PLACES = load_places()
DORMONT = PLACES["Dormont"]
DORMONT_PT = DORMONT["points"][0]
OCEAN = (0.0, -30.0)


def mk(**kw) -> Listing:
    base = {
        "source": "zillow", "source_id": "1", "url": "u", "title": "t",
        "price": 1100, "beds": 2, "baths_full": 2, "baths_half": 0,
        "area_sqft": 1000, "home_type": "SINGLE_FAMILY",
        "lat": DORMONT_PT[0], "lon": DORMONT_PT[1], "laundry": ["In Unit"],
        "parking": ["Garage"], "description": "",
    }
    base.update(kw)
    return Listing(**base)


S = Settings()


def test_full_cost_variants():
    assert score(mk(), S, PLACES).full_monthly_cost == 1100 + 160 + 85 + 15
    d = mk(description="All Utilities Included")
    assert score(d, S, PLACES).full_monthly_cost == 1115
    w = mk(description="water included")
    assert score(w, S, PLACES).full_monthly_cost == 1100 + 160 + 15
    unknown = mk(area_sqft=None)
    assert score(unknown, S, PLACES).full_monthly_cost == 1100 + 176 + 85 + 15


def test_cost_bounds_and_clamp():
    def cost_v(price):
        return score(mk(price=price, description="utilities included"), replace(S, search=replace(S.search, max_rent=9999)), PLACES).criteria["cost"][0]

    assert cost_v(1085) == 100  # full 1100
    assert cost_v(500) == 100
    assert cost_v(1785) == 0  # full 1800
    assert cost_v(3000) == 0
    assert round(cost_v(1435)) == 50  # full 1450


def test_cheap_good_scores_high_expensive_low():
    cheap = score(mk(price=1000, description="utilities included"), S, PLACES)
    pricey = score(mk(price=1490), S, PLACES)
    assert cheap.total > 90
    assert pricey.total < cheap.total - 30


def test_safety_and_income_from_place():
    r = score(mk(), S, PLACES)
    assert r.place == "Dormont"
    expect = round(100 - (0.6 * DORMONT["violent_pct"] + 0.4 * DORMONT["property_pct"]))
    assert r.criteria["safety"][0] == expect
    assert r.criteria["income"][0] == DORMONT["income_pct"]


def test_no_place_skips_criteria_but_total_computed():
    r = score(mk(lat=OCEAN[0], lon=OCEAN[1]), S, PLACES)
    assert r.place is None
    assert r.criteria["safety"][0] is None
    assert r.criteria["income"][0] is None
    w = S.score.weights
    vals = {k: v for k, (v, _) in r.criteria.items() if v is not None}
    exp = sum(v * w[k] for k, v in vals.items()) / sum(w[k] for k in vals)
    assert abs(r.total - exp) < 0.06


def test_null_pct_skips():
    places = {"X": {"violent_pct": None, "property_pct": 5, "income_pct": None,
                    "points": [[40.0, -80.0]]}}
    r = score(mk(lat=40.0, lon=-80.0), S, places)
    assert r.place == "X"
    assert r.criteria["safety"][0] is None
    assert r.criteria["income"][0] is None


def test_parking():
    def p(**kw):
        return score(mk(**kw), S, PLACES).criteria["parking"][0]

    assert p(parking=[], has_garage=True) == 100
    assert p(parking=["Garage - Attached"]) == 100
    assert p(parking=["Driveway"]) == 55
    assert p(parking=["Off Street"]) == 55
    assert p(parking=["On Street"]) == 0
    assert p(parking=[]) == 15


def test_type_and_baths():
    def c(name, **kw):
        return score(mk(**kw), S, PLACES).criteria[name][0]

    assert c("type", home_type="SINGLE_FAMILY") == 100
    assert c("type", home_type="TOWNHOUSE") == 55
    assert c("type", home_type="APARTMENT") == 30
    assert c("baths", baths_full=2, baths_half=0) == 100
    assert c("baths", baths_full=1, baths_half=1) == 80
    assert c("baths", baths_full=1, baths_half=0) == 0


def test_hard_fails():
    assert score(mk(), S, PLACES).hard_fail is None
    assert score(mk(laundry=["Shared"]), S, PLACES).hard_fail
    assert score(mk(laundry=["Coin Operated", "Hookups"]), S, PLACES).hard_fail
    assert score(mk(laundry=["In Unit"]), S, PLACES).hard_fail is None
    assert score(mk(laundry=[]), S, PLACES).hard_fail is None
    assert score(mk(price=1600), S, PLACES).hard_fail
    assert score(mk(beds=1), S, PLACES).hard_fail
    relaxed = replace(S, search=replace(S.search, in_unit_laundry=False))
    assert score(mk(laundry=["Shared"]), relaxed, PLACES).hard_fail is None


def test_outside_search_bounds_is_a_hard_fail():
    # Weirton, WV: inside FB's city radius, west of the default bounds
    weirton = mk(lat=40.4187, lon=-80.5895)
    assert score(weirton, S, PLACES).hard_fail == "outside search.bounds"
    assert score(mk(lat=40.0, lon=-79.99), S, PLACES).hard_fail == "outside search.bounds"
    assert score(mk(lat=40.7, lon=-79.99), S, PLACES).hard_fail == "outside search.bounds"
    assert score(mk(lat=40.44, lon=-79.5), S, PLACES).hard_fail == "outside search.bounds"
    assert score(mk(lat=40.44, lon=-79.99), S, PLACES).hard_fail is None


def test_missing_coordinates_do_not_fail_the_bounds_check():
    assert score(mk(lat=None, lon=None), S, PLACES).hard_fail is None


def test_bounds_come_from_settings():
    wide = replace(S, search=replace(S.search, bounds={
        "west": -81.0, "east": -79.0, "south": 40.0, "north": 41.0}))
    assert score(mk(lat=40.4187, lon=-80.5895), wide, PLACES).hard_fail is None


def test_weights_from_settings_change_total():
    base = score(mk(price=1400), S, PLACES).total
    heavy = replace(S, score=ScoreSettings(weights={"cost": 1, "safety": 12, "parking": 12,
                                                    "income": 8, "type": 7, "baths": 6}))
    assert score(mk(price=1400), heavy, PLACES).total > base
