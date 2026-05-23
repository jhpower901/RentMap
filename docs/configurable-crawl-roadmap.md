# Configurable Crawl Roadmap

## Final Goal

Make RentMap reusable for a new home-search area by changing Docker Compose environment variables, without editing Python or HTML templates.

Target capabilities:

- Set a center pin with `RENTMAP_CENTER_LAT` and `RENTMAP_CENTER_LNG`.
- Set crawl coverage with `RENTMAP_RADIUS_KM`.
- Show the configured center pin and area name in generated web pages.
- Set price filters such as max deposit and max rent from Docker Compose.
- Later, choose deal types and property types such as monthly rent, jeonse, apartment, officetel, retail, and other platform-supported categories.
- Keep current Ajou defaults when no environment variables are provided.

## Principles

- Keep CSV-based listing collection for now.
- Keep favorites in the separate persistence path already used by the server.
- Prefer a shared config layer so the scheduler, manual CLI, crawlers, and web generator agree.
- Add behavior in small steps, because each platform exposes region/type filters differently.

## Progress

- [x] Step 1: Add shared environment/CLI config for center coordinates and radius.
- [x] Step 2: Use the shared center/radius bbox in `crawl-all` and scheduled crawls.
- [x] Step 3: Add Docker Compose environment variables for location, radius, and prices.
- [ ] Step 4: Generate web runtime config for map center, center pin, and display area name.
- [ ] Step 5: Replace hardcoded Ajou labels in generated pages with configured labels.
- [ ] Step 6: Extend price config to manual platform crawls and scheduled crawls.
- [ ] Step 7: Design platform-specific mapping for deal types and property types.
- [ ] Step 8: Implement deal/property type filters per platform where supported.

## Step 1 Notes

Environment variables to introduce first:

- `RENTMAP_AREA_NAME`: display name. Default: `아주대`.
- `RENTMAP_CENTER_LAT`: default: `37.280062`.
- `RENTMAP_CENTER_LNG`: default: `127.043688`.
- `RENTMAP_RADIUS_KM`: default should preserve the current rough Ajou bbox coverage.
- `RENTMAP_MAX_DEPOSIT`: max deposit in 만원. Default: `999999`.
- `RENTMAP_MAX_RENT`: max monthly rent in 만원. Default: `999999`.

The first implementation should convert center/radius to bbox and let existing crawler code keep using `min_lat`, `max_lat`, `min_lng`, and `max_lng`.

Implemented:

- `RENTMAP_CENTER_LAT`, `RENTMAP_CENTER_LNG`, and `RENTMAP_RADIUS_KM` are read by `scripts/rentmap.py`.
- `crawl-dabang`, `crawl-zigbang`, `crawl-naver`, and `crawl-all` accept `--center-lat`, `--center-lng`, and `--radius-km`.
- `crawl-all` now passes the computed bbox to Dabang, Zigbang, and Naver.
- `docker-compose.yml` includes default location/radius env vars for both containers.
- `docker-compose.yml` includes max deposit/rent env vars for both containers.
- `crawl-dabang`, `crawl-zigbang`, `crawl-daangn`, and `crawl-all` read max deposit/rent from env by default.
- `crawl-all` runs Dabang/Zigbang/Daangn in **parallel** via `ThreadPoolExecutor`
  (Naver stays out: it's the heavy Playwright path and lives in its own container).
  Measured speedup on the 3km Ajou bbox: sequential ~19 min → parallel 10.5 min
  (dominated by Dabang's per-listing detail fetch, the slowest of the three).

Limitations after Step 1:

- Daangn still depends mainly on configured region IDs; bbox is not fully wired for collection scope.
- Generated web pages still show hardcoded Ajou labels and center pin.
- Naver collection does not yet use max deposit/rent as an upstream crawl filter.

## Open Questions

- Daangn uses region IDs, so radius-only crawling may still need configured `RENTMAP_DAANGN_REGION_IDS`.
- Naver map crawling may need URL generation or an existing map URL with center coordinates, depending on what the current crawler supports.
- Property/deal type names should be normalized internally, then mapped per platform.
