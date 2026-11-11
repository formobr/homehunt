# homehunt — plan

Owner 2026-11-11: «то же самое, что по машинам, только по домам, на Зиле в основном». Criteria come from the owner's
"Pittsburgh Rental Shortlist" artifact, with two owner changes: in-unit laundry is a hard filter, and cost carries the
highest weight.

## 1. Bootstrap

The repo starts as a renamed copy of carhunt (store, Telegram notifier, `claude -p` runner, pipeline, CLI, systemd
units, web journal, publish over ssh). Slices below replace the Facebook car parts with Zillow rental parts.
The Facebook source, fixtures and car prompts are deleted by the slice that replaces them.

## 2. Source: Zillow without login

- Search: `https://www.zillow.com/homes/for_rent/?searchQueryState=<json>`; data in `<script id="__NEXT_DATA__">` →
  `props.pageProps.searchPageState.cat1.searchResults.listResults` (41 per page, newest first with `sort=days`).
  Filters: `fr` (for rent), `mp.max` 1500, `beds.min` 2, `apa/con/apco` false (houses + townhouses only), `lau` true
  (in-unit laundry). Map bounds cover Allegheny County. Live 2026-11-11: 664 results without `lau`, 209 with it.
- Item: `homedetails/<slug>/<zpid>_zpid/`; `componentProps.gdpClientCache` (a JSON string) → first value → `property`:
  price, bedrooms, bathrooms, livingArea, homeType, address, lat/long, daysOnZillow, description,
  `resoFacts` (laundryFeatures, parkingFeatures, hasGarage, cooling, appliances, hasPetsAllowed, securityDeposit,
  availabilityDate, …), photos.
- Plain HTTP with full Chrome headers, the same `Fetcher` shape as carhunt (delay between item pages, blocked
  detection on `px-captcha`).

## 3. Score

Deterministic part, ported from the artifact (0–100 per criterion, weighted mean):

| criterion | weight | how |
|---|---|---|
| cost | 50 | full monthly = rent + utilities estimate + $15 renter insurance; linear vs the config range |
| safety | 12 | per-municipality crime index (seeded from the artifact's FBI/AreaVibes table; unknown → skipped) |
| parking | 12 | garage 100, own spot/driveway 55, street 0, unknown 15 |
| income | 8 | tract median income percentile (seeded table; unknown → skipped) |
| type | 7 | house 100, townhouse 55 |
| baths | 6 | 2 full 100, 1.5 80, 1 0 |
| walk/bike/transit | 3/1/1 | skipped (no key) unless known |

Hard filters in code: rent ≤ 1500, beds ≥ 2, house or townhouse, in-unit laundry (Zillow filter + resoFacts / AI
check; coin or shared laundry rejects).

AI (haiku triage → opus judge on candidates): reads text + photos; confirms laundry/parking/utilities, condition,
and above all **rental-scam signs** (owner abroad, deposit before viewing, price far below market, off-platform
contact). Verdict worth_seeing / doubtful / skip, a scam_risk flag, questions for the landlord.

Notify: verdict worth_seeing AND not scam AND score ≥ `notify.min_score`.

## 4. Slices

- **H1 zillow-source** — `sources/zillow.py` (search url, parse_search, parse_item, fetch_new) + `models.Listing`
  rental fields; remove fb.
- **H2 score** — `score.py` with the table above, municipality data file, config weights.
- **H3 rental-prompts** — prompts + Verdict fields for rentals (scam_risk, laundry, parking), Russian output.
- **H4 pipeline+card+web** — pipeline uses the score in the notify rule; Telegram card and web journal show rent,
  full cost, score breakdown, laundry/parking, scam flags. The journal's «best» sort orders by the score total and «cheap»
  by the full monthly cost (owner 2026-11-11: cost matters most), so the list reads in the score's order.
- **H5 deploy** — timers, optional publish over ssh (basic auth on the web server).
- **H12 fb-source** — Facebook Marketplace rentals as a second source (owner 2026-11-11).
