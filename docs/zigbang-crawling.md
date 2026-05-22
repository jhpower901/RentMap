# Zigbang crawling notes

This documents the Zigbang crawl used for the Ajou University one-room search.

## Source

- Site: https://www.zigbang.com
- Example listing supplied by the user:
  - https://www.zigbang.com/home/oneroom/items/49012411?itemDetailType=ZIGBANG
- Search target:
  - Ajou University nearby one-room monthly rentals
  - deposit <= 3000 manwon
  - rent <= 60 manwon

## Area

The crawl reuses the same map box as the Dabang pass so both sources are comparable.

| Point | Latitude | Longitude |
| --- | ---: | ---: |
| SW | 37.2736 | 127.0408 |
| NE | 37.2809 | 127.0494 |
| Center | 37.2772634 | 127.0451149 |

This box is split across two Zigbang geohash cells at precision 5:

- `wydk4`
- `wydk5`

The script requests both geohashes, deduplicates item IDs, then filters the results back to the exact bounding box.

## List API

```http
GET https://apis.zigbang.com/v2/items/oneroom
```

Query parameters used:

```text
geohash=wydk4
depositMin=0
rentMin=0
salesTypes[0]=월세
domain=zigbang
checkAnyItemWithoutFilter=true
```

Repeat the same request for `wydk5`.

Headers used:

```http
User-Agent: Mozilla/5.0
Accept: application/json, text/plain, */*
Origin: https://www.zigbang.com
Referer: https://www.zigbang.com/
```

The list API returns marker-level data, including:

- `itemId`
- `lat`
- `lng`
- `itemBmType`

## Detail API

```http
GET https://apis.zigbang.com/v3/items/{itemId}
```

Useful fields:

- `item.itemId`
- `item.salesType`
- `item.serviceType`
- `item.price.deposit`
- `item.price.rent`
- `item.manageCost.amount`
- `item.area.전용면적M2`
- `item.roomType`
- `item.floor.floor`
- `item.floor.allFloors`
- `item.roomDirection`
- `item.moveinDate`
- `item.approveDate`
- `item.residenceType`
- `item.nonCompliantBuilding`
- `item.jibunAddress`
- `item.location.lat`
- `item.location.lng`
- `item.images`
- `agent.agentTitle`
- `agent.agentName`
- `agent.agentPhone`
- `agent.agentAddress`
- `realtor.officeRegNumber`

Unlike the Dabang API observed in this project, this Zigbang detail API returned a `jibunAddress` for the tested listings. The CSV marks those addresses as `exact_jibun_from_api`.

## Re-run

From the workspace root:

```powershell
python .\scripts\crawl_zigbang.py
```

Custom output:

```powershell
python .\scripts\crawl_zigbang.py `
  --output-csv .\data\zigbang_ajou_2026-05-22.csv
```

Docker:

```powershell
.\scripts\docker.ps1 crawl-zigbang
```

## Output CSV

Default file:

```text
data/zigbang_ajou_2026-05-22.csv
```

Columns:

- source
- listing_no
- item_id
- url
- agency
- agent_name
- agent_phone
- realtor_name
- realtor_phone
- agency_address
- agency_reg_no
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
- service_type
- area_m2
- floor
- direction
- parking
- move_in
- approval_date
- residence_type
- non_compliant_building
- options
- image_1
- image_2
- crawl_note

## Notes

- Keep list filtering and final CSV filtering separate. The list API is geohash-based and may return items outside the target rectangle.
- Re-check endpoint shape before a future crawl. Zigbang's web bundle can change.
- Avoid publishing raw API JSON if it includes extra phone/contact fields that are not needed for the comparison sheet.
