# Dabang crawling notes

This documents the Dabang crawl used for the Ajou University one-room search.

## Target

- Site: https://www.dabangapp.com
- Map page used:
  - https://www.dabangapp.com/map/onetwo?m_lat=37.2772634&m_lng=127.0451149&m_zoom=18&detail_id=6a0d0f6fe445384bebb9c87c&detail_type=room
- Listing type: one-room / two-room map results
- Deal type: monthly rent
- Budget filter:
  - Deposit: 0 to 30,000,000 KRW
  - Monthly rent: 0 to 600,000 KRW

## API endpoints

### List API

```text
GET https://www.dabangapp.com/api/v5/room-list/category/one-two/bbox
```

Required query parameters:

```text
filters={JSON}
bbox={"sw":{"lat":...,"lng":...},"ne":{"lat":...,"lng":...}}
useMap=naver
zoom=18
page=1
```

Headers that mattered during the crawl:

```text
Accept: application/json, text/plain, */*
D-Api-Version: 5.0.0
D-App-Version: 1
D-Call-Type: web
csrf: token
Referer: https://www.dabangapp.com/map/onetwo
User-Agent: browser-like user agent
```

Filter JSON used:

```json
{
  "sellingTypeList": ["MONTHLY_RENT"],
  "depositRange": { "min": 0, "max": 3000 },
  "priceRange": { "min": 0, "max": 60 },
  "isIncludeMaintenance": false,
  "pyeongRange": { "min": 0, "max": 999999 },
  "useApprovalDateRange": { "min": 0, "max": 999999 },
  "roomFloorList": ["GROUND_FIRST", "GROUND_SECOND_OVER", "SEMI_BASEMENT", "ROOFTOP"],
  "roomTypeList": ["ONE_ROOM", "TWO_ROOM"],
  "dealTypeList": ["AGENT"],
  "canParking": false,
  "isShortLease": false,
  "hasElevator": false,
  "hasPano": false,
  "isDivision": false,
  "isDuplex": false
}
```

### Detail API

```text
GET https://www.dabangapp.com/api/3/new-room/detail
```

Required query parameters:

```text
room_id={room id}
api_version=3.0.1
call_type=web
version=1
```

The list API returns opaque room IDs for detail lookup. The detail API includes the public listing number, price, address, agent office, options, room detail, and image URLs.

## Search area used

The Ajou University crawl used this bounding box:

```text
southwest: lat 37.2736, lng 127.0408
northeast: lat 37.2809, lng 127.0494
zoom: 18
```

This is centered near:

```text
lat 37.2772634
lng 127.0451149
```

## Reusable script

Run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\crawl_dabang.ps1
```

Useful variants:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\crawl_dabang.ps1 `
  -MinLat 37.2736 -MinLng 127.0408 -MaxLat 37.2809 -MaxLng 127.0494 `
  -MaxDeposit 3000 -MaxRent 60 `
  -OutputCsv .\data\dabang_ajou_2026-05-22.csv
```

The script exports CSV by default. If you need the raw detail payload for debugging, pass `-RawJson .\data\some-file.raw.json`. Avoid sharing raw JSON because it can include contact-related fields that are not needed for room comparison.

## CSV columns

- source
- listing_no
- room_id
- url
- agency
- agent_name
- agent_phone
- region
- address
- latitude
- longitude
- address_public_level
- title
- deposit_manwon
- rent_manwon
- maintenance_manwon
- total_monthly_manwon
- room_type
- area_m2
- floor
- direction
- parking
- move_in
- approval_date
- building_use
- options
- security_options
- image_1
- image_2
- crawl_note

## Caveats

- Dabang can change API paths, headers, field names, or bot protections without notice.
- As of 2026-05-22, the current web app uses `GET /api/v5/room-list/category/one-two/bbox` with `bbox`, not the older `POST` shape with `location`.
- Results can include duplicate physical rooms posted by different agencies.
- Dabang often hides exact jibun/road address before contacting the agency. Treat exported coordinates as Dabang-provided map coordinates, not legally verified addresses.
- Monthly support applies to rent only, not maintenance fees or utilities.
- Before contacting an agency, check whether the listing is still active.
- Before paying a deposit, verify the register, ownership, liens, building use, tax arrears, management fee details, and exact utility billing.
