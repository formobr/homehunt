import json
import re
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from homehunt.analyze import MarketRent, Verdict
from homehunt.models import Listing
from homehunt.notify import (
    NotifyError,
    NotifyUnavailable,
    TelegramNotifier,
    facts,
    format_card,
    telegram_creds,
)
from homehunt.score import Score

TOKEN = "123456:SECRET-abc_DEF"
DETECTED = datetime(2026, 11, 10, 15, 30, tzinfo=UTC)  # 10:30 New York


def make_listing(**kw):
    base = {
        "source": "zillow",
        "source_id": "1",
        "url": "https://fb.example/item/1",
        "title": "Oak <b>&",
        "price": 4500,
        "beds": 3,
        "baths_full": 1,
        "baths_half": 1,
        "area_sqft": 1224,
        "home_type": "SINGLE_FAMILY",
        "address": "1 Oak <&> St",
        "city": "Pittsburgh",
        "description": "a < b & c",
        "photos": ["https://img.example/1.jpg"],
        "published_at": datetime(2026, 11, 10, 14, 5, tzinfo=UTC),
    }
    base.update(kw)
    return Listing(**base)


def make_verdict(**kw):
    base = {
        "verdict": "worth_seeing",
        "fit": 70,
        "value": 80,
        "scam_risk": "low",
        "scam_signs": [],
        "laundry": "in_unit",
        "parking": "garage",
        "utilities_paid_by_tenant": ["газ"],
        "summary": "s",
        "reasons_for": ["low rent <5k & clean", "second reason"],
        "risks": ["mold > bad", "old boiler"],
        "monthly_cost_note": "$500",
        "market_rent": MarketRent(low=1200, high=1500, basis="Zillow <x>"),
        "questions_for_landlord": ["Кто платит за газ?"],
        "needs_clarification_urgent": True,
        "facts_unknown": [],
        "highlights": ["отдельный вход <и> двор", "вода за счёт владельца"],
    }
    base.update(kw)
    return Verdict(**base)


def strip_own_tags(card):
    card = re.sub(r'<a href="[^"]*">', "", card)
    return card.replace("<b>", "").replace("</b>", "").replace("</a>", "")


def test_card_escapes_and_layout():
    card = format_card(make_listing(zipcode="15219"), make_verdict(), DETECTED, "new")
    lines = card.splitlines()
    assert lines[0] == "<b>1 Oak &lt;&amp;&gt; St, Pittsburgh 15219</b>"
    assert lines[1] == "$4500/мес · 3 сп · 1+1 ванн · 1224 sqft · дом"
    assert "• отдельный вход &lt;и&gt; двор" in lines
    # only our own tags may contain raw angle brackets
    assert "<" not in strip_own_tags(card)
    assert ">" not in strip_own_tags(card)
    assert "ИИ: стоит смотреть · подходит 70 · выгодность 80" in lines
    assert lines[-1].endswith("</a> · найдено 10:30")


def test_card_time_follows_report_timezone():
    la = ZoneInfo("America/Los_Angeles")
    card = format_card(make_listing(), make_verdict(), DETECTED, "new", tz=la)
    assert card.splitlines()[-1].endswith("</a> · найдено 07:30")
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    TelegramNotifier(TOKEN, "42", client_for(handler), tz=la).send_card(
        make_listing(photos=[]), make_verdict(), DETECTED, "new"
    )
    assert "найдено 07:30" in sent[0]["text"]


def test_card_price_drop_prefix():
    card = format_card(make_listing(), make_verdict(), DETECTED, "price_drop", old_price=5200)
    assert card.splitlines()[0] == "📉 Цена снижена: $5200 → $4500"


def test_compact_card_has_map_and_no_reasoning():
    listing = make_listing(
        lat=40.44, lon=-79.99, zipcode="15219", has_garage=True, parking=["Garage", "Driveway"],
        laundry=["In Unit"], cooling=["Central"], deposit=1400, pets_allowed=True,
        available_from=date(2026, 12, 1), description="pets welcome, 'secret' text",
    )
    v = make_verdict(
        summary="SUMMARYTEXT", reasons_for=["REASONTEXT"], risks=["RISKTEXT"],
        questions_for_landlord=["QUESTIONTEXT"], facts_unknown=["UNKNOWNTEXT"],
        monthly_cost_note="COSTNOTE", needs_clarification_urgent=True,
        laundry="in_unit", parking="garage",
    )
    sc = Score(
        total=72.5, full_monthly_cost=1676,
        criteria={"cost": (17.7, 50), "safety": (None, 12), "baths": (80, 5)}, place="X",
    )
    card = format_card(listing, v, DETECTED, "new", score=sc)
    lines = card.splitlines()
    assert "https://www.google.com/maps/search/?api=1&amp;query=40.44,-79.99" in lines[-1]
    assert "<b>Балл 72.5</b> · стоимость 17.7 · ванные 80" in lines
    assert "район" not in "\n".join(lines[2:4])  # skipped criterion omitted
    assert "$4500/мес · всего ≈ $1676" in lines[1]
    assert "Залог $1400 · свободен с 01.12" in lines
    assert "дом · стирка в доме · парковка гараж · кондиционер центральный" in lines
    assert not any(raw in card for raw in ("Driveway", "In Unit", "Central"))
    assert "• вода за счёт владельца" in lines
    for gone in ("SUMMARYTEXT", "REASONTEXT", "RISKTEXT", "QUESTIONTEXT", "UNKNOWNTEXT",
                 "COSTNOTE", "Разбор", "Рынок", "Спросить", "Zillow <x>"):
        assert gone not in card
    assert "питом" not in card.lower() and "pets" not in card.lower()
    assert len(card) <= 1024
    seen, handler = recorder()
    TelegramNotifier(TOKEN, "42", client_for(handler)).send_card(listing, v, DETECTED, "new", score=sc)
    kb = seen[0][1]["reply_markup"]["inline_keyboard"][0]
    assert [b["text"] for b in kb] == ["Zillow", "Карта"]
    assert kb[1]["url"] == "https://www.google.com/maps/search/?api=1&query=40.44,-79.99"


def test_map_url_without_coordinates_encodes_address_and_zip():
    card = format_card(make_listing(zipcode="15219"), make_verdict(), DETECTED, "new")
    assert "query=1%20Oak%20%3C%26%3E%20St%2015219" in card.replace("&amp;", "&")


def test_card_shows_cost_score_and_scam():
    sc = Score(
        total=72.5, full_monthly_cost=1676,
        criteria={"cost": (17.7, 50), "safety": (None, 12)}, place="Squirrel Hill",
    )
    v = make_verdict(
        scam_risk="medium", scam_signs=["просит <предоплату>"], laundry="coin", parking="street"
    )
    card = format_card(make_listing(price=1400), v, DETECTED, "new", score=sc)
    lines = card.splitlines()
    assert lines[1].startswith("$1400/мес · всего ≈ $1676 · ")
    assert "<b>Балл 72.5</b> · стоимость 17.7" in lines
    assert "⚠ риск мошенничества: просит &lt;предоплату&gt;" in lines
    assert "дом · стирка на монетах · парковка на улице" in lines
    assert not any("район" in ln for ln in lines)
    low = format_card(make_listing(), make_verdict(), DETECTED, "new", score=sc)
    assert "риск мошенничества" not in low


def test_card_button_is_named_after_the_source():
    for source, label in (("fb", "Facebook"), ("zillow", "Zillow")):
        seen, handler = recorder()
        TelegramNotifier(TOKEN, "42", client_for(handler)).send_card(
            make_listing(source=source), make_verdict(), DETECTED, "new"
        )
        kb = seen[0][1]["reply_markup"]["inline_keyboard"][0]
        assert [b["text"] for b in kb] == [label, "Карта"]


def test_facts_come_from_zillow_first():
    unknown = make_verdict(laundry="unknown", parking="unknown")
    yard = make_listing(
        home_type="TOWNHOUSE", laundry=["In Unit"], parking=["Off Street"],
        cooling=["Air Conditioner", "Central Air"],
    )
    got = dict(facts(yard, "unknown", "unknown"))
    assert got["Тип"] == "таунхаус"
    assert (got["Стирка"], got["Парковка"], got["Кондиционер"]) == (
        "в доме", "во дворе", "центральный"
    )
    card = format_card(yard, unknown, DETECTED, "new")
    assert "таунхаус · стирка в доме · парковка во дворе · кондиционер центральный" in card
    garage = make_listing(parking=["Attached", "Garage", "Off Street"], has_garage=True)
    assert [v for k, v in facts(garage) if k == "Парковка"] == ["гараж"]
    assert dict(facts(make_listing(cooling=["Wall Unit"])))["Кондиционер"] == "есть"
    bare = facts(make_listing(), "unknown", "unknown")
    assert ("Стирка", "нет данных") in bare and "Кондиционер" not in dict(bare)
    assert "стирка ? · парковка ?" in format_card(make_listing(), unknown, DETECTED, "new")
    for raw in ("In Unit", "Off Street", "Central", "Garage", "Attached", "Air Conditioner"):
        assert raw not in card


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def recorder(status_by_method=None):
    seen = []

    def handler(req):
        method = req.url.path.rsplit("/", 1)[1]
        seen.append((method, json.loads(req.content)))
        if status_by_method and method in status_by_method:
            return httpx.Response(
                status_by_method[method], json={"ok": False, "description": "bad"}
            )
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(seen)}})

    return seen, handler


def test_photo_listing_sends_short_photo_then_full_text_with_keyboard():
    # a normal card fits one caption: a single sendPhoto carrying the keyboard
    seen, handler = recorder()
    n = TelegramNotifier(TOKEN, "42", client_for(handler))
    assert n.send_card(make_listing(), make_verdict(), DETECTED, "new") == 1
    assert [m for m, _ in seen] == ["sendPhoto"]
    photo = seen[0][1]
    assert photo["photo"] == "https://img.example/1.jpg"
    assert photo["parse_mode"] == "HTML"
    assert photo["caption"] == format_card(make_listing(), make_verdict(), DETECTED, "new")
    assert photo["reply_markup"]["inline_keyboard"][0][0] == {
        "text": "Zillow", "url": "https://fb.example/item/1"
    }

    # an over-long card: photo with the first lines, the rest as one text message
    v = make_verdict(highlights=[f"fact{i} " + "z" * 100 for i in range(12)])
    full = format_card(make_listing(), v, DETECTED, "new")
    assert len(full) > 1024
    seen, handler = recorder()
    assert TelegramNotifier(TOKEN, "42", client_for(handler)).send_card(
        make_listing(), v, DETECTED, "new"
    ) == 2
    assert [m for m, _ in seen] == ["sendPhoto", "sendMessage"]
    photo, text = seen[0][1], seen[1][1]
    assert len(photo["caption"]) <= 1024 and "reply_markup" not in photo
    assert photo["caption"] + "\n" + text["text"] == full
    assert "reply_markup" in text


def test_listing_without_photo_sends_only_message():
    seen, handler = recorder()
    n = TelegramNotifier(TOKEN, "42", client_for(handler))
    n.send_card(make_listing(photos=[]), make_verdict(), DETECTED, "new")
    assert [m for m, _ in seen] == ["sendMessage"]
    assert "reply_markup" in seen[0][1]


def test_send_photo_rejected_still_sends_text_and_returns_its_id():
    seen, handler = recorder({"sendPhoto": 400})
    n = TelegramNotifier(TOKEN, "42", client_for(handler))
    assert n.send_card(make_listing(), make_verdict(), DETECTED, "new") == 2
    assert [m for m, _ in seen] == ["sendPhoto", "sendMessage"]
    assert seen[1][1]["text"] == format_card(make_listing(), make_verdict(), DETECTED, "new")
    assert "reply_markup" in seen[1][1]


@pytest.mark.parametrize("photos", [[], ["https://img.example/1.jpg"]])
def test_single_line_over_limit_is_cut_without_breaking_entities(photos):
    v = make_verdict(scam_risk="high", scam_signs=["&" * 5000])
    seen, handler = recorder()
    TelegramNotifier(TOKEN, "42", client_for(handler)).send_card(
        make_listing(photos=photos), v, DETECTED, "new"
    )
    assert seen
    for method, body in seen:
        text, limit = (
            (body["caption"], 1024) if method == "sendPhoto" else (body["text"], 4096)
        )
        assert len(text) <= limit
        assert not re.search(r"&(?!amp;|lt;|gt;)", text)


def test_429_sleeps_retry_after_once_then_raises():
    sleeps = []
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 3}})

    n = TelegramNotifier(TOKEN, "42", client_for(handler), sleep=sleeps.append)
    with pytest.raises(NotifyError):
        n.send_text("hi")
    assert sleeps == [3.0]
    assert len(calls) == 2


def test_429_then_success():
    state = {"n": 0}

    def handler(req):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, json={"parameters": {"retry_after": 1}})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 5}})

    n = TelegramNotifier(TOKEN, "42", client_for(handler), sleep=lambda s: None)
    assert n.send_text("hi") == 5


def test_token_never_leaks_in_errors():
    def handler(req):
        raise httpx.ConnectError(f"cannot reach {req.url}")

    n = TelegramNotifier(TOKEN, "42", client_for(handler))
    with pytest.raises(NotifyError) as ei:
        n.send_text("hi")
    assert "SECRET" not in str(ei.value)
    assert "bot<token>" in str(ei.value)

    def handler2(req):
        return httpx.Response(401, json={"ok": False, "description": f"bad bot{TOKEN}"})

    n = TelegramNotifier(TOKEN, "42", client_for(handler2))
    with pytest.raises(NotifyError) as ei:
        n.send_text("hi")
    assert "SECRET" not in str(ei.value)


def test_send_text_truncates_to_4096():
    sent = {}

    def handler(req):
        sent.update(json.loads(req.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    TelegramNotifier(TOKEN, "42", client_for(handler)).send_text("x" * 5000)
    assert len(sent["text"]) == 4096


def test_creds_from_env_and_empty_is_unset():
    env = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c"}
    assert telegram_creds(env) == ("t", "c")
    env["TELEGRAM_CHAT_ID"] = ""
    with pytest.raises(NotifyUnavailable):
        telegram_creds(env)


def test_creds_ignore_owner_monty_file(tmp_path, monkeypatch):
    f = tmp_path / "env"
    f.write_text("MONTY_OWNER_TG_BOT_TOKEN=ft\nMONTY_OWNER_TG_CHAT_ID=fc\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    d = tmp_path / ".config" / "monty"
    d.mkdir(parents=True)
    (d / "env").write_text("MONTY_OWNER_TG_BOT_TOKEN=a\nMONTY_OWNER_TG_CHAT_ID=b\n")
    with pytest.raises(NotifyUnavailable):
        telegram_creds({"MONTY_OWNER_ENV_FILE": str(f)})
    with pytest.raises(NotifyUnavailable):
        telegram_creds({})
