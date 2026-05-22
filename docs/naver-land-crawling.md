# Naver Land crawling notes

This documents the Naver Land crawl used for the Ajou University one-room search.

## Target

- Site: https://new.land.naver.com
- Map page used:
  - https://new.land.naver.com/rooms?ms=2AzVQ9,3zkrDJ,17&a=APT:OPST:ABYG:OBYG:GM:OR:DDDGG:JWJT:SGJT:VL&e=RETAIL&aa=SMALLSPCRENT
- Decoded map center:
  - lat 37.2777581
  - lng 127.0443067
  - zoom 17
- Filter from URL:
  - listing families: apartment, officetel, villa, house, one-room, etc.
  - price type: retail
  - additional option: small-space rent

## Why browser automation is used

Direct `Invoke-WebRequest` calls to the Naver Land article API can return `401` or `429`.
The reusable script therefore opens Chrome, enters through the Naver Land home page, and captures the same article-list JSON that the web app requests.

The script uses:

```text
GET https://new.land.naver.com/api/articles
```

The app supplies dynamic query parameters such as `cortarNo`, map bounds, zoom, filters, and authorization headers. The crawler captures the first successful list request, then requests follow-up pages by changing the `page` parameter.

## Reusable script

Run:

```powershell
python .\scripts\crawl_naver_land.py
```

Useful variants:

```powershell
python .\scripts\crawl_naver_land.py `
  --url "https://new.land.naver.com/rooms?ms=2AzVQ9,3zkrDJ,17&a=APT:OPST:ABYG:OBYG:GM:OR:DDDGG:JWJT:SGJT:VL&e=RETAIL&aa=SMALLSPCRENT" `
  --max-pages 5 `
  --output-csv .\data\naver_land_ajou_2026-05-22.csv
```

If headless Chrome is blocked or you want to watch the crawl:

```powershell
python .\scripts\crawl_naver_land.py --headed
```

If you need the raw article-list JSON:

```powershell
python .\scripts\crawl_naver_land.py `
  --raw-json .\data\naver_land_ajou_2026-05-22.raw.json
```

Docker:

```powershell
.\scripts\docker-naver.ps1 crawl-naver
```

## CSV columns

The CSV uses the same comparison-oriented columns as the Dabang export where possible:

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

- Naver Land can change app bundles, API paths, required headers, or bot protections without notice.
- The crawler captures article-list data, not every detail table field on the detail page.
- Some Naver filters in the supplied URL include broad trade types; review `tradeTypeName` and prices before comparing with monthly-rent-only Dabang data.
- Exact addresses and phone numbers may be hidden or only available after interacting with the listing.
- Before contacting an agency, check whether the listing is still active.
- Before paying a deposit, verify the register, ownership, liens, building use, tax arrears, management fee details, and exact utility billing.
