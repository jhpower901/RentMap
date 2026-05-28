# Naver Land crawling notes

This documents the Naver Land crawl used for the Ajou University one-room search.

## Target

- Site: https://new.land.naver.com
- Auto-generated map URLs (see "Coverage strategy" below):
  - `https://new.land.naver.com/rooms?ms=<lat>,<lng>,16&a=APT:OPST:ABYG:OBYG:GM:OR:DDDGG:JWJT:SGJT:VL&e=RETAIL&aa=SMALLSPCRENT&ae=ONEROOM`
  - `ms=` is base62-encoded `<lat>,<lng>,<zoom>` (the viewport center / zoom). See
    `encode_coord` / `decode_coord` in `scripts/rentmap.py`.
- Default center: `RENTMAP_CENTER_LAT` / `RENTMAP_CENTER_LNG` (Ajou Univ. defaults).
- Default radius: `RENTMAP_RADIUS_KM` (3 km).
- Filter from URL:
  - listing families: apartment, officetel, villa, house, one-room, etc.
  - price type: retail
  - additional options: small-space rent + one-room (`SMALLSPCRENT` + `ONEROOM`).

## Why browser automation is used

Direct HTTP requests to the Naver Land article API can return `401` or `429`. The
reusable script therefore opens Chrome via Playwright, enters through the Naver Land
home page, and captures the same article-list JSON that the web app requests.

Three endpoints are hit:

```text
GET https://new.land.naver.com/api/cortars?zoom=16&centerLat=X&centerLon=Y   (cortarNo lookup — fast, no auth)
GET https://new.land.naver.com/api/articles?cortarNo=...&page=N               (list — requires Authorization: Bearer JWT)
GET https://new.land.naver.com/api/articles/{articleNo}                        (detail — requires same JWT)
```

The app supplies dynamic query parameters such as `cortarNo`, map bounds, zoom,
filters, and session/authorization headers. The crawler captures the first
successful list request, then:

1. Walks `page=2..max_pages` for that `cortarNo` (until `isMoreData` is `False`).
2. Calls the detail endpoint once per bbox article to fetch the real
   address and the fields not present in the list response.

### cortarNo fast path (discovered 2026-05-28)

The `/api/cortars` endpoint returns `{"cortarNo": "4111710200", ...}` for any
`(centerLat, centerLon, zoom)` triple **without** needing an Authorization header.
This means only the **first** grid tile requires a full `page.goto()` browser
navigation (to capture the JWT). All remaining 36 tiles call `/api/cortars` via
`context.request.get()` — a lightweight HTTP call inside the existing browser
context. Result: 37-tile Ajou grid completes in ~21 s (`fast_tiles=36`).

## Coverage strategy

Naver's list API is **`cortarNo`-scoped** (dong-level administrative area), not
viewport-scoped. A `ms=` URL only steers which `cortarNo` the front-end picks; the
returned articles cover the entire dong, paginated 100 at a time.

> ⚠ **Naver's `ms=` → `cortarNo` resolution is non-deterministic.** The same
> viewport URL can resolve to different dong codes across requests — e.g. a
> tile centred on 원천동 (37.27, 127.04) sometimes lands on `4111710200`
> 원천동 and sometimes on `4113510300` 분당. Even encoding the *exact*
> coordinates of a target listing into the `ms=` does not guarantee the
> expected cortarNo. This means grid-only coverage can silently drop entire
> dongs of listings depending on Naver's mood. The `RENTMAP_NAVER_CORTARNOS`
> env var below exists exactly to make coverage deterministic.

To cover an arbitrary radius around a centre point:

1. `gen_naver_grid_urls(center_lat, center_lng, radius_km)` builds a grid of
   `ms=` tiles spaced `NAVER_TILE_STEP_KM` (~1.2 km) apart at zoom 16.
   At a 3 km radius that produces ~37 tiles. The tiles overlap by ~50 % so no
   gap between dong boundaries is missed.
2. Each tile navigation captures the resolved `cortarNo` from the API URL.
3. A `seen_cortarnos` set in `crawl_naver_async` dedups tiles that resolve to
   the same dong, so pages 2..N are paginated **only once per cortarNo**.
4. **Coverage backstop** (`_paginate_naver_cortarno`): after the grid pass,
   any cortarNo listed in `RENTMAP_NAVER_CORTARNOS` that wasn't resolved by
   any tile is paginated directly — the captured first-tile URL is reused as
   a template and only the `cortarNo` query parameter is swapped. This is
   what guarantees coverage of the dongs you care about regardless of
   Naver's randomised mapping.
5. After the list pass, every record whose lat/lng falls inside the bbox is
   enriched via `/api/articles/{articleNo}`.

You can also pin a fixed URL list via the `RENTMAP_NAVER_URLS` env var (see
below) — useful for narrowing the grid to a single area when debugging.

## Reusable script

Run:

```powershell
python .\scripts\rentmap.py crawl-naver
```

Useful variants:

```powershell
# Ajou coordinate smoke test. This must generate a grid centered near
# 37.280062,127.043688 and return non-zero rows.
python .\scripts\rentmap.py crawl-naver `
  --center-lat 37.280062 `
  --center-lng 127.043688 `
  --radius-km 3.0 `
  --skip-detail `
  --max-pages 2 `
  --output-csv .\data\naver_land_ajou_smoke.csv

# Hanyang ERICA coordinate smoke test. The ERICA center should generate the
# Naver ms= grid around 37.299900,126.837600 rather than falling back to Ajou.
python .\scripts\rentmap.py crawl-naver `
  --center-lat 37.299900 `
  --center-lng 126.837600 `
  --radius-km 3.0 `
  --skip-detail `
  --max-pages 2 `
  --output-csv .\data\naver_land_hanyang-erica_smoke.csv

# Single explicit URL (skips the auto-grid)
python .\scripts\rentmap.py crawl-naver `
  --url "https://new.land.naver.com/rooms?ms=2AzVQ9,3zkrDJ,17&a=APT:OPST:ABYG:OBYG:GM:OR:DDDGG:JWJT:SGJT:VL&e=RETAIL&aa=SMALLSPCRENT&ae=ONEROOM" `
  --max-pages 5 `
  --output-csv .\data\naver_land_ajou_2026-05-23.csv

# Watch the crawl (Chrome window opens)
python .\scripts\rentmap.py crawl-naver --headed

# Capture raw payloads for debugging / re-analysis
python .\scripts\rentmap.py crawl-naver `
  --raw-json .\data\naver_land_ajou_2026-05-23.raw.json

# Skip the per-article detail enrichment pass (faster smoke test; leaves
# placeholders like "경기도 수원시 영통구 원천동" in the address column)
python .\scripts\rentmap.py crawl-naver --skip-detail --max-pages 2
```

Docker (preferred — Playwright + Chromium are baked into `Dockerfile.naver`):

```powershell
docker compose exec rentmap-naver python /app/scripts/rentmap.py crawl-naver `
  --output-csv /app/data/naver_land_ajou_$(Get-Date -Format yyyy-MM-dd).csv `
  --raw-json   /app/data/naver_land_ajou_$(Get-Date -Format yyyy-MM-dd).raw.json
```

## Environment variables

| Variable                      | Purpose                                                                                                  |
| ----------------------------- | -------------------------------------------------------------------------------------------------------- |
| `RENTMAP_CENTER_LAT`          | Latitude for the auto-generated `ms=` grid (default `37.280062`).                                        |
| `RENTMAP_CENTER_LNG`          | Longitude for the auto-generated grid (default `127.043688`).                                            |
| `RENTMAP_RADIUS_KM`           | Radius the grid + bbox filter cover (default `3.0`).                                                     |
| `RENTMAP_NAVER_URLS`          | **Pipe-separated** (`\|`) list of full `ms=` URLs to override the auto-grid. Comma cannot be used because `ms=` itself contains commas. |
| `RENTMAP_NAVER_CORTARNOS`     | Comma-separated explicit dong-level `cortarNo` codes to **force-paginate** on top of whatever the grid resolves to. Critical: Naver's `ms=` → `cortarNo` mapping is non-deterministic (the same tile can flip dongs across runs), so without this backstop entire dongs can be silently dropped. Find codes by opening the area in `new.land.naver.com`, watching the Network tab, and reading `cortarNo=…` from the request URL. |
| `RENTMAP_MAX_DEPOSIT`         | Hard cap on deposit (만원). Applied to list API.                                                         |
| `RENTMAP_MAX_RENT`            | Hard cap on monthly rent (만원). Applied to list API.                                                    |

Example override (cover a different city's area):

```yaml
environment:
  - RENTMAP_AREA_NAME=홍대
  - RENTMAP_CENTER_LAT=37.5567
  - RENTMAP_CENTER_LNG=126.9226
  - RENTMAP_RADIUS_KM=2.0
  # Optional: pin a couple of specific viewports
  - RENTMAP_NAVER_URLS=https://new.land.naver.com/rooms?ms=...,...,16&a=...|https://new.land.naver.com/rooms?ms=...,...,16&a=...
```

## CSV columns

The Naver CSV (`naver_land_ajou_<date>.csv`) uses `NAVER_COLUMNS` defined in
`scripts/rentmap.py`. Compared to the legacy Dabang-style schema it adds
detail-API fields: `room_count`, `bathroom_count`, `room_structure`, `duplex`,
and `description`.

- source
- listing_no
- room_id
- url
- agency
- agent_name              *(detail API: `articleRealtor.representativeName`)*
- agent_phone             *(detail API: `cellPhoneNo` ‖ `representativeTelNo`)*
- region                  *(list API dong: 경기도 수원시 영통구 원천동)*
- address                 *(detail API: `exposureAddress`, e.g. 경기도 수원시 영통구 원천동 90-15)*
- latitude
- longitude
- address_public_level    *(`naver_dong_level_until_detail_enrichment` ⇒ `naver_exposure_address_from_detail_api` after enrichment)*
- title
- deposit_manwon
- rent_manwon
- maintenance_manwon
- total_monthly_manwon
- room_type
- room_count              *(detail: `articleDetail.roomCount`)*
- bathroom_count          *(detail: `articleDetail.bathroomCount`)*
- area_m2                 *(detail: `articleSpace.supplySpace`/`exclusiveSpace`, list fallback)*
- floor
- direction
- room_structure          *(detail: `articleOneroom.roomType` — 분리형/일자형/오픈형)*
- duplex                  *(detail: `articleDetail.floorLayerName` — 단층/복층)*
- parking                 *(detail: `parkingPossibleYN` + `parkingCount` ⇒ `가능 (4대)` / `불가`)*
- move_in                 *(detail: `moveInPossibleYmd` or `moveInTypeName`)*
- approval_date           *(detail: `articleFacility.buildingUseAprvYmd`, list fallback)*
- building_use
- description             *(detail: `articleDetail.detailDescription` — full body)*
- options                 *(detail union: `tagList` + life/aircon/room facilities)*
- security_options        *(detail: `securityFacilities` + `buildingFacilities`)*
- image_1                 *(detail: `articlePhotos[0].imageSrc`, list fallback)*
- image_2                 *(detail: `articlePhotos[1].imageSrc`)*
- crawl_note              *(records which API filled the row)*

## Detail-API response shape

Top-level keys in `GET /api/articles/{articleNo}`:

```
articleDetail / articleAddition / articleFacility / articleFloor /
articleNoneHscp / articlePrice / articleRealtor / articleSpace /
articleTax / articleOneroom / articleExistTabs / articlePhotos /
articleBuildingRegister / landPrice / administrationCostInfo / isVrExposed
```

`scripts/rentmap.py:enrich_from_naver_detail` only reads the subset listed in
the column table above. If you need a new field, extract it there and add a
matching column to `NAVER_COLUMNS`.

## Caveats

- Naver Land can change app bundles, API paths, required headers, or bot
  protections without notice.
- The list API never returns the exact jibun (`detailAddressYn=N` for almost
  every article). The detail pass is what fills `address`; if it's skipped
  (`--skip-detail`) you'll only see the dong-level region.
- Detail enrichment fires N HTTP calls per crawl (~1000 for a 3 km Ajou radius
  at ~250 ms each ≈ 5 min). Watch the scheduler timeout if you widen the
  radius significantly.
- Some Naver filters in the supplied URL include broad trade types; review
  `tradeTypeName` and prices before comparing with monthly-rent-only Dabang
  data.
- The detail endpoint exposes `articleRealtor.cellPhoneNo` /
  `representativeTelNo` but Naver still gates these behind
  `isCellPhoneExposure` / `isRepresentativeTelExposure` flags — when set
  to `False` the strings are empty.
- Before contacting an agency, check whether the listing is still active.
- Before paying a deposit, verify the register, ownership, liens, building use,
  tax arrears, management fee details, and exact utility billing.
