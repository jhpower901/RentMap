# Daangn (당근 부동산) crawling notes

This documents the Karrot Real Estate crawl used for the Ajou University one-room search.

## Target

- Site: https://realty.daangn.com / https://www.daangn.com/kr/realty/
- Listing type: one-room / two-room monthly rent
- Budget filter:
  - Deposit: 0 to 30,000,000 KRW
  - Monthly rent: 0 to 600,000 KRW

## Data source: SSR page + RELAY_STORE

Unlike Dabang which has a JSON list API, Karrot Real Estate embeds data in two ways:

### 1. Listing page SSR (`window.__remixContext`)

The page `https://www.daangn.com/kr/realty/?in=x-{REGION_ID}` embeds all listings for a region in the server-side rendered HTML inside `window.__remixContext`. The path is:

```
ctx.state.loaderData['routes/kr.realty._index'].realtyPosts.realtyPosts
```

Fields available from the listing page: `title`, `images`, `salesType`, `trades`, `area`, `floor`, `address`, `region`, `manageCost`, `buildingApprovalDate`, `writerType`, `chatRoomCount`, `watchCount`, `webUrl`, `status`, `content`.

### 2. Article detail page (`window.RELAY_STORE`)

The page `https://realty.daangn.com/articles/{ARTICLE_ID}` embeds Relay store data as a JSON string in `window.RELAY_STORE`. The store is a normalized flat object; some fields use `__ref` pointers to other nodes.

Additional fields from the detail page: `publicCoordinate` (lat/lon), `publicAddress`, `roomCnt`, `bathroomCnt`, `subwayStations`, `writerTypeV2`, `buildingApprovalDate`.

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
powershell -ExecutionPolicy Bypass -File .\scripts\crawl_daangn.ps1
```

Useful variants:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\crawl_daangn.ps1 `
  -MaxDeposit 3000 -MaxRent 60 `
  -OutputCsv .\data\daangn_ajou_2026-05-22.csv

# Skip detail page fetches (faster, no coordinates):
powershell -ExecutionPolicy Bypass -File .\scripts\crawl_daangn.ps1 -SkipDetail

# Custom region list:
powershell -ExecutionPolicy Bypass -File .\scripts\crawl_daangn.ps1 `
  -RegionIds @(1289, 1290, 1298)
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
- image_1
- image_2
- crawl_note

## Caveats

- Karrot can change their SSR page structure, route keys, or RELAY_STORE format without notice.
- As of 2026-05-22, the listing page uses `routes/kr.realty._index` as the loader data key.
- The SSR page returns a fixed snapshot; listings added after page load are not included.
- Most listings are one page (no pagination observed). If a region has many listings, older ones may be cut off.
- Karrot listings include both agency (BROKER) and individual owner (DIRECT_USER) postings. Only BROKER listings are comparable to Dabang/Zigbang agency listings.
- Coordinates from `publicCoordinate` are approximate map positions, not verified addresses.
- Before visiting or contacting, verify the listing is still active on the website.
- Before paying a deposit, verify the register, ownership, liens, building use, and tax arrears.
