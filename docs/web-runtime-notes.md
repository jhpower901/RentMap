# Web runtime notes

Date: 2026-05-22

This note records the current web-page data loading and image rendering decisions so the same issues can be understood later.

## Current Web Data Flow

The platform pages are static HTML files under `web/`.

The pages currently use inline `RAW` listing arrays generated from CSV data. The future direction is to move the data source behind a DB/API-like boundary, but the current static build still needs to work without a DB server.

Relevant files:

- `web/dabang.html`
- `web/daangn.html`
- `web/zigbang.html`
- `web/naver.html`
- `scripts/_tpl_platform.html`
- `web/listing-data-source.js`
- `web/data/*.csv`

## Issue: Only Zigbang Was Visible

Symptom:

- Dabang, Daangn, and Naver showed `0건`.
- Their maps had no listing markers.
- Zigbang still showed listings.

Cause:

- Several generated platform pages had a literal `\n` string immediately after `const RAW = [`.
- That made the inline JavaScript invalid, so the page script stopped before:
  - type filters were populated
  - map markers were created
  - table rows were rendered

Fix:

- Replace the literal `\n` after `const RAW = [` with a real valid JavaScript array start.
- Apply the same generation-safe behavior in `scripts/_tpl_platform.html`.

Validation after fix:

```text
Dabang: 115 rows, 116 Leaflet interactive elements
Daangn: 11 rows, 12 Leaflet interactive elements
Zigbang: 56 rows, 57 Leaflet interactive elements
Naver: 28 rows, 101 Leaflet interactive elements
```

## Issue: Zigbang Images Did Not Load

Symptom:

- Zigbang listing rows rendered, but thumbnail images stayed blank or failed.

Cause:

- Zigbang CDN image URLs in the CSV looked like:

```text
https://ic.zigbang.com/ic/items/{item_id}/1.jpg
```

- Those original URLs returned an error from the CDN.
- Adding resize/query parameters made the same image load correctly.

Fix:

- Add `imageSrc(src)`.
- For Zigbang CDN images only, convert:

```text
https://ic.zigbang.com/ic/items/{item_id}/1.jpg
```

to:

```text
https://ic.zigbang.com/ic/items/{item_id}/1.jpg?w=400&h=300&q=70
```

Notes:

- This conversion is intentionally source-specific.
- Other platform image URLs are left unchanged.
- Popup images and table thumbnails both use `imageSrc()`.

## Image Loading Strategy

Problem:

- Image downloads can be slow.
- Listing rows and map markers should not wait for image downloads.

Current behavior:

1. Render table rows and map markers first.
2. Put thumbnail URLs in `data-src` instead of `src`.
3. Call `scheduleImageLoad(tbody)` after rows are appended.
4. During browser idle time, move `data-src` into `src`.
5. Images appear as they finish loading.

Key helper:

```js
function scheduleImageLoad(container) {
  const load = () => {
    container.querySelectorAll('img[data-src]').forEach(img => {
      img.src = img.dataset.src;
      img.removeAttribute('data-src');
    });
  };
  if ('requestIdleCallback' in window) requestIdleCallback(load, { timeout: 800 });
  else setTimeout(load, 0);
}
```

Why this helps:

- Table rows appear immediately.
- Map markers appear immediately.
- Filtering and sorting are not blocked by slow image downloads.
- Images still load automatically shortly after render.

## Important Implementation Detail

Do not call `scheduleImageLoad(tbody)` inside helpers like `sortData()` or `getFiltered()`.

It should run only after the DOM rows have been appended in `render()`.

Expected pattern:

```js
function render() {
  const filtered = sortData(getFiltered(), sortCol, sortAsc);
  const tbody = document.getElementById('tbody');
  tbody.innerHTML = '';

  filtered.forEach(r => {
    const tr = document.createElement('tr');
    const imgCell = r.img1
      ? '<img class="img-thumb" data-src="' + imageSrc(r.img1) + '" loading="lazy" decoding="async" alt="">'
      : '<div class="no-img">사진없음</div>';
    // append row...
    tbody.appendChild(tr);
  });

  scheduleImageLoad(tbody);
}
```

## Future DB/API Migration Notes

When replacing CSV/static data with a DB-backed API:

- Keep the UI rendering contract stable.
- Return normalized listing objects with fields equivalent to the current page shape:
  - `id`
  - `source`
  - `url`
  - `agency`
  - `phone`
  - `region`
  - `address`
  - `lat`
  - `lon`
  - `title`
  - `deposit`
  - `rent`
  - `maint`
  - `total`
  - `type`
  - `area`
  - `floor`
  - `img1`
  - `img2`
- Keep source-specific image normalization either:
  - in the API response layer, or
  - in the existing frontend `imageSrc()` helper.

For long-term maintainability, prefer the API to return already-normalized image URLs, while keeping `imageSrc()` as a defensive frontend fallback.

