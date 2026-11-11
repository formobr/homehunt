# homehunt

Rental alerts from Zillow and Facebook Marketplace, scored by cost and checked by Claude.

Every 15 minutes homehunt reads the newest Zillow and Facebook Marketplace rentals in your area **without logging in**, scores each new
one with a transparent formula where the full monthly cost weighs the most, asks Claude to check the text and
photos (laundry, parking, condition, **rental-scam signs**), and sends only the strong ones to your Telegram.
Everything is kept in a local SQLite journal and a self-contained HTML page.

- **No Anthropic API key.** Claude runs through the [Claude Code](https://claude.com/claude-code) CLI
  (`claude -p`) on your own Claude subscription: `haiku` triages every listing, `opus` (with web search for
  comparable rents) re-checks the candidates.
- **No Zillow or Facebook account.** Public search and listing pages are fetched with plain HTTP at a polite pace.
- **No database server.** One SQLite file, `data/homehunt.db`, created on the first run.

Defaults: Allegheny County, PA (Pittsburgh); house or townhouse; 2+ bedrooms; rent ≤ $1500; washer and dryer in
the unit. All of it is configurable.

## What you get in Telegram

One short card per listing that passes, with the photo and two buttons, «Zillow» (or «Facebook») and «Карта» (Google Maps):

```
1236 Prospect Ave APT 2, Pittsburgh 15234
$1050/мес · всего ≈ $1262 · 2 сп · 1 ванн · 700 sqft · таунхаус
Балл 72.8 · стоимость 98 · район 97 · парковка 15 · доход 36 · тип 55 · ванные 0
ИИ: стоит смотреть · подходит 66 · выгодность 72
таунхаус · стирка в доме · парковка ? · кондиционер есть
свободен с 03.11
• стиральная и сушильная машина в квартире
• отдельный задний вход
• гранитная кухня с посудомойкой
• рядом T и магазины
• оконные кондиционеры
📍 Карта · найдено 09:35
```

- **Facts** (type, laundry, parking, garage, A/C, deposit, move-in date) come straight from Zillow's listing data;
  the AI only fills a gap Zillow leaves empty.
- **Балл** is the weighted score (0–100) with each criterion's value.
- **ИИ** is Claude's verdict: how well it fits and how good the price is for what you get.
- **⚠ риск мошенничества** appears only when Claude sees scam signs (landlord «abroad», deposit before a viewing,
  price far below similar listings, contact only off Zillow).

The text is Russian by default; change the language line in `prompts/analyze.md` and the labels in
`src/homehunt/notify.py` for another language.

An alert is sent only when **all** hold: score ≥ `notify.min_score`, AI verdict «worth seeing», fit ≥
`notify.min_reliability`, value ≥ `notify.min_value`, scam risk not high, laundry not shared/coin/hookups.
Listings over budget, too small or with shared laundry are filtered out **before** any AI call.

## How the score works

| criterion | default weight | value |
|---|---|---|
| cost | 50 | full monthly cost = rent + utilities estimate ($0.16/sqft + $85 water, 0 if included) + $15 renter insurance; 100 at `score.cost_best`, 0 at `score.cost_worst`, linear |
| safety | 12 | crime index of the nearest known municipality (FBI / AreaVibes, `src/homehunt/data/places.json`); skipped if unknown |
| parking | 12 | garage 100, own spot / driveway 55, street 0, unknown 15 |
| income | 8 | area median household income percentile; skipped if unknown |
| type | 7 | house 100, townhouse 55 |
| baths | 6 | 2 full 100, 1.5 80, 1 0 |

`places.json` covers Pittsburgh and Allegheny County suburbs. For another area, replace it (same shape: crime
percentiles, income percentile and reference points per place) or leave it — safety and income are then skipped
and the score is cost + parking + type + baths.

## Requirements

- Linux with systemd (user timers) — macOS works for manual runs (`homehunt poll`) without the timers.
- Python 3.12+.
- [Claude Code](https://claude.com/claude-code) installed and logged in with your subscription (`claude` on `PATH`).
- A Telegram bot (free, from [@BotFather](https://t.me/BotFather)).

## Install

```bash
git clone https://github.com/formobr/homehunt.git
cd homehunt
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env
```

1. **Telegram.** Create a bot with @BotFather, put its token into `.env` as `TELEGRAM_BOT_TOKEN`. Send `/start`
   to the bot, then get your chat id:
   ```bash
   curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | grep -o '"chat":{"id":[-0-9]*'
   ```
   and put it into `.env` as `TELEGRAM_CHAT_ID`.
2. **Your area and budget.** Edit `config.yaml` (`search.bounds` is the map rectangle, `max_rent`, `min_beds`,
   `in_unit_laundry`) — or override any key from `.env`, see below. Set `score.cost_best` / `cost_worst` around
   the real full costs in your area (rent + ~$250–300).
3. **Your needs.** `prompts/criteria.md` is what the AI checks; rewrite it in plain words.
4. **Try one run by hand:**
   ```bash
   .venv/bin/homehunt poll
   ```
   The first run rates the backlog, `poll.max_details_per_run` listings per run (~1 min each).
5. **Run it every 15 minutes:**
   ```bash
   bash deploy/install.sh
   ```
   Stop and remove the timers with `bash deploy/uninstall.sh`.

## Settings

`config.yaml` holds the defaults. Any key can be overridden in `.env` (or the environment) as
`HOMEHUNT__<SECTION>__<KEY>=<value>`; the value is parsed as YAML, so numbers, `true/false`, `[lists]` and
`{maps}` work. An unknown section or key is an error, not a silent no-op. `.env.example` lists every key.

| setting | default | meaning |
|---|---|---|
| `search.bounds` | Allegheny County | `{west, east, south, north}` map rectangle |
| `search.region` | Allegheny County, PA (Pittsburgh) | area name given to the AI (prompts); set it to your area along with `search.bounds` |
| `search.fb_city` | `pittsburgh` | Facebook Marketplace city slug (`facebook.com/marketplace/<slug>/propertyrentals`); listings outside `search.bounds` are filtered out |
| `search.max_rent` | 1500 | rent limit, USD/month |
| `search.min_beds` | 2 | bedrooms |
| `search.in_unit_laundry` | `true` | require washer/dryer in the unit |
| `score.cost_best` / `cost_worst` | 1250 / 1850 | full monthly cost that scores 100 / 0 |
| `score.weights` | see table above | `{cost, safety, parking, income, type, baths}` |
| `poll.interval_min` | 15 | timer interval (re-run `deploy/install.sh` after changing it) |
| `poll.max_details_per_run` | 12 | listings opened and rated per run (keeps a run under the 25 min service timeout) |
| `notify.min_score` | 55 | score bar |
| `notify.min_reliability` / `min_value` | 60 / 60 | AI fit / value bar |
| `notify.price_drop_min_usd` / `_pct` | 200 / 5 | what counts as a rent drop |
| `ai.triage_model` / `judge_model` | `haiku` / `opus` | Claude models for the two stages |
| `ai.max_photos` | 8 | photos the AI reads per listing |
| `report.timezone` | `America/New_York` | IANA zone for times in cards and the journal; the daily summary arrives at 09:00 in this zone (re-run `deploy/install.sh` after changing it); an invalid zone is an error |

Examples: `HOMEHUNT__SEARCH__MAX_RENT=1800`,
`HOMEHUNT__SCORE__WEIGHTS={cost: 60, safety: 10, parking: 12, income: 6, type: 6, baths: 6}`.

## Another city

Everything area-specific is in settings (or `.env` overrides):

1. `search.bounds` — the map rectangle to search: `{west, east, south, north}` in degrees. Easiest way: open
   Zillow rentals for your city in a browser, look at the map, and take the corner coordinates (or use any
   «bounding box» tool such as bboxfinder.com).
2. `search.region` — how the AI should call your area, e.g. `Austin, TX`. The AI uses it for the local market,
   climate and commute.
3. `search.max_rent`, `search.min_beds`, `search.in_unit_laundry` — your needs.
4. `score.cost_best` / `cost_worst` — set them around real full monthly costs in your area: roughly the cheapest
   and the most expensive rents you would consider plus ~$250–300 for utilities and insurance.
5. `report.timezone` — your IANA time zone, e.g. `America/Chicago`; re-run `bash deploy/install.sh` after it.
6. Safety and income use `src/homehunt/data/places.json`, which covers only Allegheny County. Elsewhere these two
   criteria are simply skipped and the score is cost + parking + type + baths; or fill `places.json` for your area
   (per place: crime percentiles, income percentile, a few reference `[lat, lon]` points).

```bash
# .env
HOMEHUNT__SEARCH__REGION=Austin, TX
HOMEHUNT__SEARCH__BOUNDS={west: -97.94, east: -97.56, south: 30.10, north: 30.52}
HOMEHUNT__SEARCH__MAX_RENT=1900
HOMEHUNT__SCORE__COST_BEST=1600
HOMEHUNT__SCORE__COST_WORST=2300
HOMEHUNT__REPORT__TIMEZONE=America/Chicago
```

## Journal

```bash
.venv/bin/homehunt log --limit 30      # newest listings with verdict, score and full cost
.venv/bin/homehunt show <id>           # everything about one listing, full AI answer
.venv/bin/homehunt stats --days 7      # counts, latency, AI usage
.venv/bin/homehunt web --out data/web/index.html   # the HTML journal page
```
**See it in the browser (local, no server needed).** After every run the timer rebuilds the page at
`data/web/index.html` (one self-contained HTML file: filters, sorting, photos, full analysis). Open it:

```bash
python3 -m http.server 8000 --bind 127.0.0.1 -d data/web
```

then go to http://localhost:8000 — reload to see new runs. (Opening `data/web/index.html` directly as a file
works too.)

**Optional: your own web server.** To have the page on a domain after every run, set `HOMEHUNT_WEB_SSH` (an ssh host
from your `~/.ssh/config`) and `HOMEHUNT_WEB_DIR` (a directory it serves) in `.env`, and protect that directory with
basic auth, e.g. for nginx:

```nginx
location ^~ /homehunt/ {
    auth_basic "homehunt";
    auth_basic_user_file /etc/nginx/.htpasswd-homehunt;
    try_files $uri $uri/index.html =404;
}
```

Leave `HOMEHUNT_WEB_SSH` empty (the default) and the page is only written locally.

## Costs and limits

- Each listing that passes the filters costs one `haiku` call; candidates add one `opus` call. That is billed
  against your Claude subscription's usage limits, not money per call.
- Zillow or Facebook may change a page format or start showing a captcha or login wall; homehunt then sends a
  «returns nothing» alert naming that source. The other source keeps working.

## Development

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q
```

Tests never touch the network or `claude`: HTTP, the Claude runner and Telegram are injected and faked, and the
parsers are tested against saved pages in `tests/fixtures/` (phone numbers replaced with test values).

Sibling project for used cars: [carhunt](https://github.com/formobr/carhunt).

Use it for your own search, at a polite pace, and in line with the terms of the sites you read.
