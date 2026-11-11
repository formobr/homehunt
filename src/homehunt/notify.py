"""Telegram delivery: credentials, card formatting, and the notifier."""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime, tzinfo
from typing import Any
from urllib.parse import quote

import httpx

from homehunt.analyze import Verdict
from homehunt.config import ReportSettings
from homehunt.models import Listing
from homehunt.score import Score

API = "https://api.telegram.org"
TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024

_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_\-]+")


class NotifyError(Exception):
    pass


class NotifyUnavailable(NotifyError):
    pass


def redact(text: str) -> str:
    return _TOKEN_RE.sub("bot<token>", text)


def telegram_creds(env: dict[str, str] | None = None) -> tuple[str, str]:
    env = os.environ if env is None else env
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    chat = env.get("TELEGRAM_CHAT_ID", "")
    if token and chat:
        return token, chat
    raise NotifyUnavailable("telegram credentials not configured")


def _esc(value: object) -> str:
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _clip(value: object, n: int) -> str:
    s = " ".join(str(value).split())
    return s if len(s) <= n else s[: max(n - 1, 0)] + "…"


def _hhmm(dt: datetime, tz: tzinfo | None = None) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(tz or ReportSettings().tz).strftime("%H:%M")


HOME_TYPE_RU = {
    "SINGLE_FAMILY": "дом",
    "TOWNHOUSE": "таунхаус",
    "MULTI_FAMILY": "многоквартирный",
    "CONDO": "кондо",
    "APARTMENT": "квартира",
}

LAUNDRY_RU = {
    "in_unit": "в квартире",
    "hookups": "только подключение",
    "shared": "общая",
    "coin": "монетная",
    "unknown": "неизвестно",
}
PARKING_RU = {
    "garage": "гараж",
    "driveway": "подъездная дорожка",
    "street": "на улице",
    "unknown": "неизвестно",
}
CRITERION_RU = {
    "cost": "стоимость",
    "safety": "район",
    "parking": "парковка",
    "income": "доход",
    "type": "тип",
    "baths": "ванные",
}
VERDICT_RU = {
    "worth_seeing": "стоит смотреть",
    "doubtful": "сомнительно",
    "skip": "пропустить",
}


def _num(x: float) -> str:
    return f"{x:g}"


def map_url(listing: Listing) -> str:
    if listing.lat is not None and listing.lon is not None:
        query = f"{listing.lat},{listing.lon}"
    else:
        text = " ".join(p for p in (listing.address or listing.title, listing.zipcode) if p)
        query = quote(text, safe="")
    return f"https://www.google.com/maps/search/?api=1&query={query}"


NO_DATA = "нет данных"
LAUNDRY_FALLBACK = {
    "in_unit": "в доме",
    "hookups": "только подключения",
    "shared": "общая",
    "coin": "на монетах",
}
PARKING_FALLBACK = {"garage": "гараж", "driveway": "во дворе", "street": "на улице"}


def _laundry_ru(values: list[str]) -> str | None:
    text = " ".join(values).lower().replace("-", " ")
    if "in unit" in text:
        return "в доме"
    if "hookup" in text:
        return "только подключения"
    if "shared" in text or "common" in text or "laundry room" in text:
        return "общая"
    if "coin" in text:
        return "на монетах"
    return None


def _parking_ru(listing: Listing) -> str | None:
    text = " ".join(listing.parking).lower()
    if listing.has_garage or any(k in text for k in ("garage", "attached", "detached", "carport")):
        return "гараж"
    if any(k in text for k in ("driveway", "off street", "open", "assigned", "private")):
        return "во дворе"
    if "on street" in text:
        return "на улице"
    return None


def facts(
    listing: Listing, laundry: str = "unknown", parking: str = "unknown"
) -> list[tuple[str, str]]:
    """Russian facts about the home: Zillow fields first, the verdict only as fallback."""
    out: list[tuple[str, str]] = []
    if listing.home_type:
        out.append(("Тип", HOME_TYPE_RU.get(listing.home_type, "другое")))
    out.append(
        ("Стирка", _laundry_ru(listing.laundry) or LAUNDRY_FALLBACK.get(laundry, NO_DATA))
    )
    out.append(("Парковка", _parking_ru(listing) or PARKING_FALLBACK.get(parking, NO_DATA)))
    if listing.cooling:
        central = any("central" in c.lower() for c in listing.cooling)
        out.append(("Кондиционер", "центральный" if central else "есть"))
    if listing.deposit is not None:
        out.append(("Залог", f"${listing.deposit}"))
    if listing.available_from is not None:
        out.append(("Свободен с", listing.available_from.strftime("%d.%m")))
    return out


def _facts_line(listing: Listing, verdict: Verdict) -> str:
    parts = []
    for label, value in facts(listing, verdict.laundry, verdict.parking):
        if label == "Тип":
            parts.append(value)
        elif label in ("Стирка", "Парковка", "Кондиционер"):
            parts.append(f"{label.lower()} {'?' if value == NO_DATA else value}")
    return " · ".join(parts)


def format_card(
    listing: Listing,
    verdict: Verdict,
    detected_at: datetime,
    reason: str,
    old_price: int | None = None,
    score: Score | None = None,
    tz: tzinfo | None = None,
) -> str:
    """One compact alert: facts, short scores, the AI label, highlights, map link."""
    lines: list[str] = []
    if reason == "price_drop" and old_price is not None:
        lines.append(f"📉 Цена снижена: ${old_price} → ${listing.price}")

    place = ", ".join(
        p for p in (listing.address, " ".join(x for x in (listing.city, listing.zipcode) if x)) if p
    )
    lines.append(f"<b>{_esc(_clip(place or listing.title, 120))}</b>")

    money = [f"${listing.price}/мес"]
    if score is not None:
        money.append(f"всего ≈ ${score.full_monthly_cost}")
    if listing.beds is not None:
        money.append(f"{listing.beds} сп")
    if listing.baths_full is not None:
        baths = f"{listing.baths_full}" + (f"+{listing.baths_half}" if listing.baths_half else "")
        money.append(f"{baths} ванн")
    if listing.area_sqft:
        money.append(f"{listing.area_sqft} sqft")
    if listing.home_type:
        money.append(_esc(HOME_TYPE_RU.get(listing.home_type, listing.home_type)))
    lines.append(" · ".join(money))

    if score is not None:
        parts = [f"<b>Балл {_num(score.total)}</b>"]
        for key, (val, _weight) in score.criteria.items():
            if val is not None:
                parts.append(f"{CRITERION_RU.get(key, _esc(key))} {_num(val)}")
        lines.append(" · ".join(parts))

    lines.append(
        f"ИИ: {VERDICT_RU[verdict.verdict]} · подходит {verdict.fit} · выгодность {verdict.value}"
    )
    if verdict.scam_risk in ("medium", "high"):
        signs = "; ".join(_esc(s) for s in verdict.scam_signs) or verdict.scam_risk
        lines.append(f"⚠ риск мошенничества: {signs}")

    lines.append(_esc(_facts_line(listing, verdict)))

    extra = []
    if listing.deposit is not None:
        extra.append(f"Залог ${listing.deposit}")
    if listing.available_from is not None:
        extra.append(f"свободен с {listing.available_from.strftime('%d.%m')}")
    if extra:
        lines.append(" · ".join(extra))

    lines += [f"• {_esc(_clip(h, 120))}" for h in verdict.highlights if h.strip()]
    lines.append(f'📍 <a href="{_esc(map_url(listing))}">Карта</a> · найдено {_hhmm(detected_at, tz)}')
    return "\n".join(lines)


def _cut_line(line: str, limit: int) -> list[str]:
    """Last resort for one line longer than limit: cut at a space, never inside &entity;."""
    parts: list[str] = []
    while len(line) > limit:
        cut = line.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
            amp = line.rfind("&", 0, cut)
            if amp > 0 and ";" not in line[amp:cut]:
                cut = amp
        parts.append(line[:cut])
        line = line[cut:]
    parts.append(line)
    return parts


def split_text(text: str, limit: int = TEXT_LIMIT) -> list[str]:
    """Split on line boundaries into chunks of at most `limit` chars."""
    chunks: list[str] = []
    cur = ""
    for line in text.split("\n"):
        for piece in _cut_line(line, limit):
            if cur and len(cur) + 1 + len(piece) > limit:
                chunks.append(cur)
                cur = piece
            else:
                cur = f"{cur}\n{piece}" if cur else piece
    chunks.append(cur)
    return chunks


class TelegramNotifier:
    def __init__(
        self,
        token: str,
        chat_id: str,
        client: httpx.Client,
        sleep: Callable[[float], None] = time.sleep,
        tz: tzinfo | None = None,
    ) -> None:
        self._tz = tz
        self._token = token
        self._chat_id = chat_id
        self._client = client
        self._sleep = sleep

    def _post(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{API}/bot{self._token}/{method}"
        for attempt in (0, 1):
            try:
                resp = self._client.post(url, json=payload)
            except httpx.HTTPError as exc:
                raise NotifyError(redact(f"{method} failed: {exc}")) from None
            try:
                body = resp.json()
            except ValueError:
                body = {}
            if resp.status_code == 429:
                if attempt == 1:
                    raise NotifyError(f"{method}: rate limited (429)")
                retry = (body.get("parameters") or {}).get("retry_after", 1)
                self._sleep(float(retry))
                continue
            return body if isinstance(body, dict) else {}
        raise NotifyError(f"{method}: rate limited (429)")  # pragma: no cover

    def _message_id(self, method: str, body: dict[str, Any]) -> int:
        if not body.get("ok"):
            raise NotifyError(redact(f"{method}: {body.get('description', 'not ok')}"))
        return int(body["result"]["message_id"])

    def _send_texts(self, chunks: list[str], keyboard: dict[str, Any]) -> int:
        message_id = 0
        for i, chunk in enumerate(chunks):
            payload: dict[str, Any] = {
                "chat_id": self._chat_id,
                "text": chunk,
                "parse_mode": "HTML",
            }
            if i == len(chunks) - 1:
                payload["reply_markup"] = keyboard
            message_id = self._message_id("sendMessage", self._post("sendMessage", payload))
        return message_id

    def send_card(
        self,
        listing: Listing,
        verdict: Verdict,
        detected_at: datetime,
        reason: str,
        old_price: int | None = None,
        score: Score | None = None,
    ) -> int:
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "Zillow", "url": listing.url},
                    {"text": "Карта", "url": map_url(listing)},
                ]
            ]
        }
        card = format_card(listing, verdict, detected_at, reason, old_price, score, self._tz)
        rest = card
        if listing.photos:
            caption = split_text(card, CAPTION_LIMIT)[0]
            rest = card[len(caption):].strip()
            payload: dict[str, Any] = {
                "chat_id": self._chat_id,
                "photo": listing.photos[0],
                "caption": caption,
                "parse_mode": "HTML",
            }
            if not rest:
                payload["reply_markup"] = keyboard
            # The photo is decoration: its failure must not stop the text.
            try:
                photo_id = self._message_id("sendPhoto", self._post("sendPhoto", payload))
            except NotifyError:
                rest = card
            else:
                if not rest:
                    return photo_id
        return self._send_texts(split_text(rest), keyboard)

    def send_text(self, text: str) -> int:
        body = self._post(
            "sendMessage", {"chat_id": self._chat_id, "text": text[:TEXT_LIMIT]}
        )
        return self._message_id("sendMessage", body)
