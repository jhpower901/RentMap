# Daangn (당근 부동산) crawling notes

This documents the Karrot Real Estate crawl used for the Ajou University one-room search.

## Target

- Site: https://realty.daangn.com / https://www.daangn.com/kr/realty/
- Listing type: one-room / two-room monthly rent
- Budget filter:
  - Deposit: 0 to 30,000,000 KRW
  - Monthly rent: 0 to 600,000 KRW

## Data source: SSR listing page + GraphQL detail API

### 1. Listing page SSR (`window.__remixContext`)

The page `https://www.daangn.com/kr/realty/?in=x-{REGION_ID}` embeds all listings for a region in the server-side rendered HTML inside `window.__remixContext`. The path is:

```
ctx.state.loaderData['routes/kr.realty._index'].realtyPosts.realtyPosts
```

Fields available from the listing page: `title`, `images`, `salesType`, `trades`, `area`, `floor`, `address`, `region`, `manageCost`, `buildingApprovalDate`, `writerType`, `chatRoomCount`, `watchCount`, `webUrl`, `status`, `content`.

### 2. Article detail via GraphQL persisted query (discovered 2026-05-28)

**Instead of scraping the SSR detail HTML**, the crawler now calls the GraphQL endpoint directly:

```
POST https://realty.kr.karrotmarket.com/graphql
operationName: ArticleDetailQuery
variables: {"articleId": "<id>"}
extensions: {"persistedQuery": {"version": 1, "sha256Hash": "<hash>"}}
```

The hash for `ArticleDetailQuery` was found by scanning the Relay JS bundles from `realty.daangn.com/assets/`. It is stored as `DAANGN_ARTICLE_DETAIL_QUERY_HASH` in `scripts/rentmap.py`.

Detail fields returned by GraphQL: `publicCoordinate` (lat/lon), `publicAddress`,
`roomCount`, `bathroomCount`, `buildingApprovalDate`, `buildingOrientation` (e.g. `SOUTH_FACING`),
`buildingUsage` (e.g. `SINGLE_FAMILY_HOUSING`), `moveInDate`, `options[]` (PARKING/ELEVATOR/…),
`includeManageCostOptionV3[]` (maintenance inclusions), `manageCostChargeType` (FIXED/ACTUAL),
`bizProfile.name` (agency name), `description`.

All `ENUM` values are translated to Korean via mapping tables in `scripts/rentmap.py`:
- `DAANGN_ORIENTATION_MAP` — 동향/서향/남향/북향/…
- `DAANGN_BUILDING_USAGE_MAP` — 단독주택/공동주택/오피스텔/…
- `DAANGN_OPTION_LABEL_MAP` — 주차/엘리베이터/세탁기/전기레인지/다락방/…
- `DAANGN_MANAGE_COST_OPTION_MAP` — 수도/전기/가스/인터넷/…

Detail fetches run in parallel via `ThreadPoolExecutor(max_workers=DAANGN_GQL_WORKERS)`.
402 articles now complete in ~25 s (was ~80 s with sequential SSR scraping).

## Region IDs near Ajou University

Karrot uses numeric region IDs. Key IDs for the Ajou University area:

| ID   | Dong               | Gu/Si               |
|------|--------------------|---------------------|
| 1289 | 우만1동             | 수원시 팔달구         |
| 1290 | 우만2동             | 수원시 팔달구         |
| 1291 | 인계동              | 수원시 팔달구         |
| 1294 | 매탄1동             | 수원시 영통구         |
| 1295 | 매탄2동             | 수원시 영통구         |
| 1296 | 매탄3동             | 수원시 영통구         |
| 1297 | 매탄4동             | 수원시 영통구         |
| 1298 | 원천동              | 수원시 영통구         |

These IDs were discovered by calling the `MapRegionQuery` persisted GraphQL operation with sequential region IDs and matching coordinates near lat 37.28, lon 127.04.

Region IDs can be used in URLs as: `?in=DONG_NAME-REGION_ID` (e.g., `?in=우만1동-1289`).

## Sales type values

Karrot uses different salesType codes than Dabang:

| Karrot code      | Meaning         |
|------------------|-----------------|
| `SPLIT_ONE_ROOM` | 분리형 원룸      |
| `OPEN_ONE_ROOM`  | 오픈형 원룸      |
| `TWO_ROOM`       | 투룸             |
| `OFFICETEL`      | 오피스텔         |
| `APART`          | 아파트           |
| `STORE`          | 상가             |

## Reusable script

Run:

```powershell
python .\scripts\rentmap.py crawl-daangn
```

Useful variants:

```powershell
python .\scripts\rentmap.py crawl-daangn `
  --max-deposit 3000 --max-rent 60 `
  --output-csv .\data\daangn_ajou_2026-05-22.csv

# Skip detail page fetches (faster, no coordinates):
python .\scripts\rentmap.py crawl-daangn --skip-detail

# Custom region list:
python .\scripts\rentmap.py crawl-daangn `
  --region-ids 1289 1290 1298
```

Docker:

```powershell
.\scripts\docker.ps1 crawl-daangn
```

## CSV columns

- source
- listing_no
- url
- writer_type (`BROKER` or `DIRECT_USER`)
- region_depth1 (e.g., 경기도)
- region_depth2 (e.g., 수원시 팔달구)
- region_depth3 (e.g., 우만1동)
- address
- latitude
- longitude
- title
- deposit_manwon
- rent_manwon
- maintenance_manwon
- total_monthly_manwon
- room_type
- room_count
- area_m2
- floor
- approval_date
- direction             *(GraphQL `buildingOrientation` → 동향/남향/…)*
- parking              *(GraphQL `options[PARKING].value` → 가능/불가능)*
- elevator             *(GraphQL `options[ELEVATOR].value` → 있음/없음)*
- pet_allowed          *(GraphQL `options[PET].value` → 가능/불가능)*
- loan_available       *(GraphQL `options[MORTGAGE].value` → 가능/불가능)*
- building_use         *(GraphQL `buildingUsage` → 단독주택/공동주택/…)*
- move_in              *(GraphQL `moveInDate`)*
- maintenance_basis    *(GraphQL `manageCostChargeType`: FIXED/ACTUAL)*
- maintenance_items    *(GraphQL `includeManageCostOptionV3`: 수도; 전기; 가스; …)*
- options              *(GraphQL `options[]` YES values translated via DAANGN_OPTION_LABEL_MAP)*
- description          *(GraphQL `description` — full free-text body)*
- image_1
- image_2
- crawl_note

## Caveats

- Karrot can change their SSR page structure, route keys, or GraphQL persisted-query hashes without notice. If `ArticleDetailQuery` starts returning `PersistedQueryKeyNotFound`, re-run `scripts/_scan_daangn_bundles.py` to extract the updated hash and update `DAANGN_ARTICLE_DETAIL_QUERY_HASH` in `scripts/rentmap.py`.
- As of 2026-05-28, the listing page uses `routes/kr.realty._index` as the loader data key; the GraphQL endpoint is `realty.kr.karrotmarket.com/graphql`.
- The SSR page returns a fixed snapshot; listings added after page load are not included.
- Most listings are one page (no pagination observed). If a region has many listings, older ones may be cut off.
- Karrot listings include both agency (BROKER) and individual owner (DIRECT_USER) postings. Only BROKER listings are comparable to Dabang/Zigbang agency listings.
- Coordinates from `publicCoordinate` are approximate map positions, not verified addresses.
- Before visiting or contacting, verify the listing is still active on the website.
- Before paying a deposit, verify the register, ownership, liens, building use, and tax arrears.
