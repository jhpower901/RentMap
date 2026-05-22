# Data normalization audit

Date: 2026-05-22

This note reviews whether the crawled CSV files are ready to be consumed dynamically by the web page and, later, stored in a database.

## Summary

The current CSV files are partially normalized, but not yet consistent enough to use as a stable DB-facing schema.

- Dabang and Zigbang are close to normalized.
- Naver Land currently has a collection-area problem: the file is named for Ajou, but the rows are from the Bundang Jeongja area.
- Daangn has a different schema and a much wider collection area.
- The combined CSV currently includes only Dabang and Zigbang.

Recommended direction:

1. Keep raw platform CSV files as source snapshots.
2. Generate one normalized file, for example `normalized_listings.csv`.
3. Let the web page and future DB import read only the normalized file.

## Files Reviewed

| File | Rows | Status |
|---|---:|---|
| `data/dabang_ajou_2026-05-22.csv` | 115 | Mostly normalized |
| `data/zigbang_ajou_2026-05-22.csv` | 56 | Mostly normalized, with extra source-specific fields |
| `data/naver_land_ajou_2026-05-22.csv` | 100 | Not usable as Ajou data yet |
| `data/daangn_ajou_2026-05-22.csv` | 276 | Needs normalization and geographic filtering |
| `data/ajou_rentals_combined_2026-05-22.csv` | 141 | Normalized subset, Dabang + Zigbang only |

## Platform Findings

### Dabang

File: `data/dabang_ajou_2026-05-22.csv`

Status: mostly normalized.

- Uses the expected common 30-column schema.
- No duplicate `source + listing_no` IDs found.
- Coordinates are within the rough Ajou bounding box.
- Price fields are numeric and consistently in `manwon`.

Observed coordinate range:

```text
lat: 37.2732618870758 to 37.2797964243647
lon: 127.0386172659 to 127.0486412216
inside rough Ajou bbox: 115 / 115
```

### Zigbang

File: `data/zigbang_ajou_2026-05-22.csv`

Status: mostly normalized, but wider than the common schema.

- Uses the common fields, plus source-specific fields such as:
  - `item_id`
  - `realtor_name`
  - `realtor_phone`
  - `agency_address`
  - `agency_reg_no`
  - `service_type`
  - `residence_type`
  - `non_compliant_building`
- No duplicate `source + listing_no` IDs found.
- Coordinates are within the rough Ajou bounding box.
- One row has a blank `address`.

Observed coordinate range:

```text
lat: 37.273276396622 to 37.279403834521
lon: 127.038605310993 to 127.04923576076
inside rough Ajou bbox: 56 / 56
```

### Naver Land

File: `data/naver_land_ajou_2026-05-22.csv`

Status: not ready.

The main issue is not schema shape but collection scope. The CSV is named as Ajou data, but the coordinates indicate Bundang Jeongja-area listings.

Observed coordinate range:

```text
lat: 37.357701 to 37.373299
lon: 127.104845 to 127.122766
inside rough Ajou bbox: 0 / 100
```

Additional normalization issues:

- `agent_phone` is blank for all rows.
- `region` is blank for all rows.
- `maintenance_manwon` is blank for all rows.
- `rent_manwon` is blank for 22% of rows, mostly lease-only rows.
- `image_1` is blank for 68% of rows.
- `area_m2` is not numeric. It stores composite values such as `63/25`, likely supply/exclusive area.

Recommended fixes:

- Re-run the Naver crawler with a verified Ajou URL/map center.
- Split `area_m2` into `supply_area_m2` and `exclusive_area_m2`.
- Preserve trade type so lease-only rows can be distinguished from monthly-rent rows.

### Daangn

File: `data/daangn_ajou_2026-05-22.csv`

Status: needs normalization and filtering.

The schema differs from the common listing schema:

- Missing common fields:
  - `agency`
  - `agent_phone`
  - `region`
- Has Daangn-specific fields:
  - `writer_type`
  - `region_depth1`
  - `region_depth2`
  - `region_depth3`
  - `room_count`

Observed coordinate range:

```text
lat: 37.248163696913984 to 37.294477494321995
lon: 127.01634491843001 to 127.06541812553002
inside rough Ajou bbox: 29 / 276
```

This suggests the Daangn collection area is much wider than Ajou. That may be intentional, but it should not be mixed into an Ajou-only normalized dataset without filtering.

Recommended fixes:

- Map `writer_type` into a normalized `agency_type` or `seller_type`.
- Derive `region` from `region_depth2 + region_depth3`.
- Add blank-compatible `agency` and `agent_phone` fields if unavailable.
- Apply a geographic filter before including rows in Ajou-focused views.

### Combined CSV

File: `data/ajou_rentals_combined_2026-05-22.csv`

Status: normalized subset.

- Contains 141 rows.
- Includes only:
  - `dabang`
  - `zigbang`
- Does not include:
  - `naver_land`
  - `daangn`
- All rows are inside the rough Ajou bounding box.

Observed coordinate range:

```text
lat: 37.2736355334 to 37.2796191456
lon: 127.041304515359 to 127.04923576076
inside rough Ajou bbox: 141 / 141
```

## Cross-File Normalization Issues

### Listing ID

Current ID fields differ by platform:

- Dabang: `listing_no`, `room_id`
- Zigbang: `listing_no`, `item_id`
- Naver Land: `listing_no`, `room_id`
- Daangn: `listing_no`

Recommended DB-facing fields:

```text
source
source_listing_id
source_room_id
```

The stable unique key should be:

```text
source + source_listing_id
```

### Source Names

The CSV and web-generated JS do not always use the same source names.

Example:

- CSV: `naver_land`
- Web JS: `naver`

Recommended fix:

Use one canonical value everywhere:

```text
dabang
zigbang
naver_land
daangn
```

### Area

`area_m2` is not consistent.

- Dabang/Zigbang: usually a single numeric area.
- Naver Land: composite value such as `63/25`.
- Daangn: numeric, but one blank value exists.

Recommended DB-facing fields:

```text
area_m2
supply_area_m2
exclusive_area_m2
area_raw
```

### Price

Most files use `manwon` consistently, but trade type is not explicit enough across platforms.

Recommended DB-facing fields:

```text
trade_type
deposit_manwon
rent_manwon
maintenance_manwon
total_monthly_manwon
```

For lease-only rows, `rent_manwon` and `total_monthly_manwon` should be null.

### Region and Address

Address precision differs by platform.

- Dabang often exposes only dong-level address.
- Zigbang can expose exact jibun from API.
- Naver Land currently has blank `region`.
- Daangn has region depth fields but no normalized `region`.

Recommended DB-facing fields:

```text
region
address
address_public_level
latitude
longitude
```

### Contact and Agency

Agent/agency data varies significantly.

Recommended DB-facing fields:

```text
agency
agent_name
agent_phone
agency_type
```

For Daangn, `writer_type` can map to `agency_type`.

## Proposed Normalized Schema

Use this as the web/DB-facing listing schema:

```text
source
source_listing_id
source_room_id
url
trade_type
agency
agent_name
agent_phone
agency_type
region
address
address_public_level
latitude
longitude
title
deposit_manwon
rent_manwon
maintenance_manwon
total_monthly_manwon
room_type
room_count
area_m2
supply_area_m2
exclusive_area_m2
area_raw
floor
direction
parking
move_in
approval_date
building_use
residence_type
non_compliant_building
options
security_options
image_1
image_2
crawl_note
crawled_at
```

## Recommended Next Step

Create a normalization script that reads the platform CSVs and emits:

```text
data/normalized_listings_2026-05-22.csv
```

The script should:

1. Preserve raw platform CSVs unchanged.
2. Convert all platforms into the proposed normalized schema.
3. Apply a configurable geographic filter for Ajou-focused views.
4. Split Naver `area_m2` composite values into supply/exclusive area.
5. Normalize source names.
6. Emit validation warnings for missing coordinates, invalid prices, duplicate IDs, and out-of-bounds rows.

After that, the web page can load one normalized CSV dynamically instead of relying on pre-generated `data_*.js` files.
