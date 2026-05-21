# Generates web/ HTML pages from the four platform CSVs.
# Run from the workspace root:
#   powershell -ExecutionPolicy Bypass -File .\scripts\gen_web.ps1

param(
    [string]$DataDir = ".\data",
    [string]$OutDir  = ".\web"
)

$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

# ---------------------------------------------------------------------------
# CSV -> normalised JS-object helpers
# ---------------------------------------------------------------------------

function Esc($v) {
    # JSON-escape a string value for embedding in a JS string literal
    if ($null -eq $v) { return "" }
    return "$v" -replace '\\', '\\' -replace '"', '\"' -replace "`r`n", '\n' -replace "`n", '\n' -replace "`r", '\n'
}

function ToNum($v) {
    if ([string]::IsNullOrWhiteSpace($v)) { return "null" }
    $d = 0.0
    if ([double]::TryParse($v, [ref]$d)) { return "$d" }
    return "null"
}

function ToJsObj($h) {
    # $h is a hashtable of field->value
    $parts = foreach ($kv in $h.GetEnumerator()) {
        $k = $kv.Key
        $v = $kv.Value
        if ($v -eq "null" -or $v -match '^-?[0-9]+(\.[0-9]+)?$') {
            "`"$k`":$v"
        } else {
            "`"$k`":`"$(Esc $v)`""
        }
    }
    return "{" + ($parts -join ",") + "}"
}

function NormalDabang($r) {
    return ToJsObj @{
        source    = "dabang"
        id        = $r.listing_no
        url       = $r.url
        agency    = $r.agency
        phone     = $r.agent_phone
        region    = $r.region
        address   = $r.address
        lat       = (ToNum $r.latitude)
        lon       = (ToNum $r.longitude)
        title     = $r.title
        deposit   = (ToNum $r.deposit_manwon)
        rent      = (ToNum $r.rent_manwon)
        maint     = (ToNum $r.maintenance_manwon)
        total     = (ToNum $r.total_monthly_manwon)
        type      = $r.room_type
        area      = $r.area_m2
        floor     = $r.floor
        img1      = $r.image_1
        img2      = $r.image_2
    }
}

function NormalDaangn($r) {
    $agency = if ($r.writer_type -eq "DIRECT_USER") { "직거래" } else { "중개사" }
    $region = @($r.region_depth2, $r.region_depth3) | Where-Object { $_ -ne "" } | Select-Object -First 2
    return ToJsObj @{
        source    = "daangn"
        id        = $r.listing_no
        url       = $r.url
        agency    = $agency
        phone     = ""
        region    = ($region -join " ")
        address   = $r.address
        lat       = (ToNum $r.latitude)
        lon       = (ToNum $r.longitude)
        title     = $r.title
        deposit   = (ToNum $r.deposit_manwon)
        rent      = (ToNum $r.rent_manwon)
        maint     = (ToNum $r.maintenance_manwon)
        total     = (ToNum $r.total_monthly_manwon)
        type      = $r.room_type
        area      = $r.area_m2
        floor     = $r.floor
        img1      = $r.image_1
        img2      = $r.image_2
    }
}

function NormalZigbang($r) {
    return ToJsObj @{
        source    = "zigbang"
        id        = $r.listing_no
        url       = $r.url
        agency    = $r.agency
        phone     = $r.agent_phone
        region    = $r.region
        address   = $r.address
        lat       = (ToNum $r.latitude)
        lon       = (ToNum $r.longitude)
        title     = $r.title
        deposit   = (ToNum $r.deposit_manwon)
        rent      = (ToNum $r.rent_manwon)
        maint     = (ToNum $r.maintenance_manwon)
        total     = (ToNum $r.total_monthly_manwon)
        type      = $r.room_type
        area      = $r.area_m2
        floor     = $r.floor
        img1      = $r.image_1
        img2      = $r.image_2
    }
}

function NormalNaver($r) {
    return ToJsObj @{
        source    = "naver"
        id        = $r.listing_no
        url       = $r.url
        agency    = $r.agency
        phone     = $r.agent_phone
        region    = $r.region
        address   = $r.address
        lat       = (ToNum $r.latitude)
        lon       = (ToNum $r.longitude)
        title     = $r.title
        deposit   = (ToNum $r.deposit_manwon)
        rent      = (ToNum $r.rent_manwon)
        maint     = (ToNum $r.maintenance_manwon)
        total     = (ToNum $r.total_monthly_manwon)
        type      = $r.room_type
        area      = $r.area_m2
        floor     = $r.floor
        img1      = $r.image_1
        img2      = $r.image_2
    }
}

function ToJsArray($rows, $normFn) {
    $objs = $rows | ForEach-Object { & $normFn $_ }
    return "[\n" + ($objs -join ",\n") + "\n]"
}

# ---------------------------------------------------------------------------
# Load CSVs
# ---------------------------------------------------------------------------
$dabang  = Import-Csv "$DataDir\dabang_ajou_2026-05-22.csv"  -Encoding UTF8
$daangn  = Import-Csv "$DataDir\daangn_ajou_2026-05-22.csv"  -Encoding UTF8
$zigbang = Import-Csv "$DataDir\zigbang_ajou_2026-05-22.csv" -Encoding UTF8
$naver   = Import-Csv "$DataDir\naver_land_ajou_2026-05-22.csv" -Encoding UTF8

$jsDabang  = ToJsArray $dabang  ${function:NormalDabang}
$jsDaangn  = ToJsArray $daangn  ${function:NormalDaangn}
$jsZigbang = ToJsArray $zigbang ${function:NormalZigbang}
$jsNaver   = ToJsArray $naver   ${function:NormalNaver}

# ---------------------------------------------------------------------------
# Shared CSS + JS fragments
# ---------------------------------------------------------------------------

$SHARED_CSS = @'
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Noto Sans KR',sans-serif;font-size:14px;background:#f5f7fa;color:#222}
a{color:inherit;text-decoration:none}
nav{display:flex;align-items:center;gap:4px;padding:10px 16px;background:#fff;border-bottom:1px solid #e0e0e0;flex-wrap:wrap}
nav .brand{font-weight:700;font-size:16px;margin-right:8px}
nav a{padding:5px 12px;border-radius:20px;font-size:13px;white-space:nowrap}
nav a:hover{background:#f0f0f0}
nav a.active{background:#222;color:#fff}
.page{max-width:1400px;margin:0 auto;padding:16px}
h1{font-size:20px;font-weight:700;margin-bottom:12px}
.badge{display:inline-block;padding:2px 8px;border-radius:12px;font-size:12px;font-weight:600;color:#fff}
.badge-dabang{background:#FF5C38}
.badge-daangn{background:#FF6F00}
.badge-zigbang{background:#6366F1}
.badge-naver{background:#03C75A}
.controls{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px;align-items:center}
.controls label{font-size:13px;display:flex;align-items:center;gap:4px}
.controls input[type=range]{width:100px}
.controls select,.controls input[type=text]{padding:5px 8px;border:1px solid #ccc;border-radius:6px;font-size:13px}
.count{font-size:13px;color:#666;margin-left:auto}
.layout{display:grid;grid-template-columns:1fr 380px;gap:12px}
@media(max-width:900px){.layout{grid-template-columns:1fr}}
.map-box{height:500px;border-radius:10px;overflow:hidden;border:1px solid #ddd;position:sticky;top:16px}
table{width:100%;border-collapse:collapse;background:#fff;border-radius:10px;overflow:hidden;box-shadow:0 1px 4px #0001}
th{background:#f0f0f0;padding:8px 10px;text-align:left;font-size:12px;white-space:nowrap;cursor:pointer;user-select:none}
th:hover{background:#e4e4e4}
td{padding:7px 10px;border-top:1px solid #f0f0f0;vertical-align:middle;font-size:13px}
tr:hover td{background:#fafafa}
.img-thumb{width:64px;height:48px;object-fit:cover;border-radius:4px;cursor:pointer}
.no-img{width:64px;height:48px;background:#eee;border-radius:4px;display:flex;align-items:center;justify-content:center;font-size:10px;color:#999}
.price{font-weight:600}
.total{color:#d00;font-weight:700}
.agency{font-size:12px;color:#555}
.link-btn{display:inline-block;padding:3px 8px;border:1px solid #ccc;border-radius:4px;font-size:12px}
.link-btn:hover{background:#eee}
.leaflet-popup-content{font-size:13px;line-height:1.6;min-width:200px}
.popup-title{font-weight:700;margin-bottom:4px}
.popup-price{color:#d00;font-weight:700}
.popup-img{width:100%;max-height:120px;object-fit:cover;border-radius:4px;margin:4px 0}
.note-warn{background:#fff3cd;border:1px solid #ffc107;border-radius:6px;padding:8px 12px;font-size:13px;margin-bottom:12px}
'@

$SHARED_JS = @'
function sortTable(data, col, asc) {
  return [...data].sort((a, b) => {
    let va = a[col], vb = b[col];
    if (va === null) va = asc ? Infinity : -Infinity;
    if (vb === null) vb = asc ? Infinity : -Infinity;
    if (typeof va === 'string') return asc ? va.localeCompare(vb,'ko') : vb.localeCompare(va,'ko');
    return asc ? va - vb : vb - va;
  });
}
function fmtDeposit(v) {
  if (v === null || v === undefined) return '-';
  if (v >= 10000) return (v/10000).toFixed(1).replace(/\.0$/,'') + '억';
  return v + '만';
}
function fmtRent(v) {
  if (v === null || v === undefined) return '-';
  return v + '만';
}
function fmtArea(v) {
  if (!v) return '-';
  const n = parseFloat(v);
  return isNaN(n) ? v : n.toFixed(1) + 'm²';
}
'@

# ---------------------------------------------------------------------------
# Per-platform page template
# ---------------------------------------------------------------------------

function Write-PlatformPage {
    param(
        [string]$File,
        [string]$Title,
        [string]$Source,
        [string]$AccentColor,
        [string]$JsData,
        [string]$ExtraNote = ""
    )

    $activeClass = @{dabang="";daangn="";zigbang="";naver=""}
    $activeClass[$Source] = ' class="active"'

    $html = @"
<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>$Title - 아주대 월세 매물</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;600;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
$SHARED_CSS
</style>
</head>
<body>
<nav>
  <span class="brand">아주대 월세</span>
  <a href="index.html">지도</a>
  <a href="dabang.html"$($activeClass['dabang'])><span class="badge badge-dabang">다방</span></a>
  <a href="daangn.html"$($activeClass['daangn'])><span class="badge badge-daangn">당근</span></a>
  <a href="zigbang.html"$($activeClass['zigbang'])><span class="badge badge-zigbang">직방</span></a>
  <a href="naver.html"$($activeClass['naver'])><span class="badge badge-naver">네이버</span></a>
</nav>
<div class="page">
  <h1><span class="badge badge-$Source" style="font-size:16px;padding:4px 12px">$Title</span> 매물 목록</h1>
  $ExtraNote
  <div class="controls">
    <label>보증금 최대 <input type="number" id="maxDeposit" value="3000" min="0" max="10000" step="100" style="width:80px"> 만원</label>
    <label>월세 최대 <input type="number" id="maxRent" value="60" min="0" max="200" step="5" style="width:60px"> 만원</label>
    <label>방 유형 <select id="typeFilter"><option value="">전체</option></select></label>
    <input type="text" id="search" placeholder="검색어..." style="min-width:120px">
    <span class="count" id="countLabel">0건</span>
  </div>
  <div class="layout">
    <div>
      <table id="tbl">
        <thead>
          <tr>
            <th data-col="img">사진</th>
            <th data-col="deposit" data-num="1">보증금</th>
            <th data-col="rent" data-num="1">월세</th>
            <th data-col="maint" data-num="1">관리비</th>
            <th data-col="total" data-num="1">총월납부</th>
            <th data-col="type">방유형</th>
            <th data-col="area">면적</th>
            <th data-col="floor">층</th>
            <th data-col="address">주소</th>
            <th data-col="agency">중개사무소</th>
            <th>링크</th>
          </tr>
        </thead>
        <tbody id="tbody"></tbody>
      </table>
    </div>
    <div>
      <div class="map-box" id="map"></div>
    </div>
  </div>
</div>
<script>
$SHARED_JS

const RAW = $JsData;
const SOURCE = "$Source";
const ACCENT = "$AccentColor";

// populate type filter
const types = [...new Set(RAW.map(r=>r.type).filter(Boolean))].sort((a,b)=>a.localeCompare(b,'ko'));
const sel = document.getElementById('typeFilter');
types.forEach(t => { const o = document.createElement('option'); o.value=t; o.textContent=t; sel.appendChild(o); });

// map
const map = L.map('map').setView([37.2769, 127.0435], 15);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '&copy; OpenStreetMap contributors', maxZoom:19
}).addTo(map);

const markers = [];
RAW.forEach(r => {
  if (!r.lat || !r.lon) return;
  const m = L.circleMarker([r.lat, r.lon], {
    radius:7, fillColor:ACCENT, color:'#fff', weight:2, fillOpacity:0.85
  }).addTo(map);
  const img = r.img1 ? `<img class="popup-img" src="${r.img1}" onerror="this.style.display='none'">` : '';
  m.bindPopup(`<div class="popup-title">${r.title||''}</div>
${img}
<div class="popup-price">보증금 ${fmtDeposit(r.deposit)} / 월세 ${fmtRent(r.rent)}</div>
<div>관리비: ${fmtRent(r.maint)} | 총: ${fmtRent(r.total)}</div>
<div class="agency">${r.agency||''} ${r.phone||''}</div>
<div>${r.address||''}</div>
<a href="${r.url}" target="_blank" class="link-btn" style="margin-top:4px;display:inline-block">매물 보기</a>`);
  markers.push({marker:m, data:r});
});

let sortCol = 'rent', sortAsc = true;

function getFiltered() {
  const maxDep = parseFloat(document.getElementById('maxDeposit').value)||9999;
  const maxRent = parseFloat(document.getElementById('maxRent').value)||999;
  const type = document.getElementById('typeFilter').value;
  const q = document.getElementById('search').value.toLowerCase();
  return RAW.filter(r => {
    if (r.deposit !== null && r.deposit > maxDep) return false;
    if (r.rent !== null && r.rent > maxRent) return false;
    if (type && r.type !== type) return false;
    if (q) {
      const hay = [r.title, r.address, r.agency, r.type].join(' ').toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
}

function render() {
  const filtered = sortTable(getFiltered(), sortCol, sortAsc);
  document.getElementById('countLabel').textContent = filtered.length + '건';
  const visibleIds = new Set(filtered.map(r=>r.id));

  // update markers
  markers.forEach(({marker, data}) => {
    if (visibleIds.has(data.id)) {
      marker.setStyle({fillOpacity:0.85, opacity:1});
    } else {
      marker.setStyle({fillOpacity:0.2, opacity:0.3});
    }
  });

  const tbody = document.getElementById('tbody');
  tbody.innerHTML = '';
  filtered.forEach(r => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${r.img1 ? `<img class="img-thumb" src="${r.img1}" alt="" onerror="this.parentNode.innerHTML='<div class=\\'no-img\\'>사진없음</div>'">` : '<div class="no-img">사진없음</div>'}</td>
      <td class="price">${fmtDeposit(r.deposit)}</td>
      <td class="price">${fmtRent(r.rent)}</td>
      <td>${fmtRent(r.maint)}</td>
      <td class="total">${fmtRent(r.total)}</td>
      <td>${r.type||'-'}</td>
      <td>${fmtArea(r.area)}</td>
      <td>${r.floor||'-'}</td>
      <td>${r.address||r.region||'-'}</td>
      <td class="agency">${r.agency||'-'}${r.phone ? '<br><span style="color:#888">'+r.phone+'</span>' : ''}</td>
      <td><a href="${r.url}" target="_blank" class="link-btn">보기</a></td>
    `;
    tr.addEventListener('click', () => {
      const m = markers.find(x=>x.data.id===r.id);
      if (m) { map.flyTo([r.lat, r.lon], 17, {duration:0.5}); m.marker.openPopup(); }
    });
    tbody.appendChild(tr);
  });
}

// sort headers
document.querySelectorAll('th[data-col]').forEach(th => {
  th.addEventListener('click', () => {
    const col = th.dataset.col;
    if (sortCol === col) { sortAsc = !sortAsc; } else { sortCol = col; sortAsc = true; }
    document.querySelectorAll('th').forEach(t => t.textContent = t.textContent.replace(/ [▲▼]$/,''));
    th.textContent = th.textContent + (sortAsc ? ' ▲' : ' ▼');
    render();
  });
});

['maxDeposit','maxRent','typeFilter','search'].forEach(id => {
  document.getElementById(id).addEventListener('input', render);
});

render();
</script>
</body>
</html>
"@
    [System.IO.File]::WriteAllText("$OutDir\$File", $html, [System.Text.Encoding]::UTF8)
    Write-Host "Wrote $OutDir\$File"
}

# ---------------------------------------------------------------------------
# Write per-platform pages
# ---------------------------------------------------------------------------

Write-PlatformPage -File "dabang.html" -Title "다방" -Source "dabang" `
    -AccentColor "#FF5C38" -JsData $jsDabang

Write-PlatformPage -File "daangn.html" -Title "당근부동산" -Source "daangn" `
    -AccentColor "#FF6F00" -JsData $jsDaangn

Write-PlatformPage -File "zigbang.html" -Title "직방" -Source "zigbang" `
    -AccentColor "#6366F1" -JsData $jsZigbang

$naverNote = '<div class="note-warn">⚠️ 네이버부동산 데이터는 아주대 근처가 아닌 <b>분당(정자역) 일대</b> 매물을 포함합니다. 지도의 기본 중심도 분당으로 설정됩니다.</div>'
Write-PlatformPage -File "naver.html" -Title "네이버부동산" -Source "naver" `
    -AccentColor "#03C75A" -JsData $jsNaver -ExtraNote $naverNote

# ---------------------------------------------------------------------------
# Combined map page (index.html)
# ---------------------------------------------------------------------------

$allJs = "const ALL_DATA = {
  dabang:  $jsDabang,
  daangn:  $jsDaangn,
  zigbang: $jsZigbang,
  naver:   $jsNaver
};"

$indexHtml = @"
<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>아주대 월세 매물 통합지도</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;600;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
$SHARED_CSS
html,body{height:100%}
.map-full{height:calc(100vh - 48px);width:100%}
.map-controls{position:absolute;top:60px;left:10px;z-index:1000;background:#fff;border-radius:10px;padding:12px;box-shadow:0 2px 10px #0002;min-width:220px;max-height:calc(100vh - 80px);overflow-y:auto}
.map-controls h2{font-size:15px;font-weight:700;margin-bottom:10px}
.map-controls label{display:flex;align-items:center;gap:6px;margin-bottom:6px;font-size:13px;cursor:pointer}
.map-controls input[type=checkbox]{width:15px;height:15px;accent-color:#222}
.price-row{display:flex;gap:6px;flex-direction:column;margin-bottom:8px}
.price-row label{font-size:13px;margin-bottom:2px}
.price-row input{width:100%;padding:4px 6px;border:1px solid #ccc;border-radius:4px;font-size:13px}
.stats{font-size:12px;color:#666;border-top:1px solid #eee;padding-top:8px;margin-top:8px}
.stat-row{display:flex;justify-content:space-between;margin-bottom:3px}
.source-links{border-top:1px solid #eee;padding-top:8px;margin-top:8px;display:flex;flex-direction:column;gap:4px}
.source-links a{display:flex;align-items:center;gap:6px;font-size:13px;padding:4px 6px;border-radius:6px}
.source-links a:hover{background:#f5f5f5}
</style>
</head>
<body>
<nav>
  <span class="brand">아주대 월세</span>
  <a href="index.html" class="active">지도</a>
  <a href="dabang.html"><span class="badge badge-dabang">다방</span></a>
  <a href="daangn.html"><span class="badge badge-daangn">당근</span></a>
  <a href="zigbang.html"><span class="badge badge-zigbang">직방</span></a>
  <a href="naver.html"><span class="badge badge-naver">네이버</span></a>
</nav>
<div class="map-controls">
  <h2>통합 필터</h2>
  <div>
    <label><input type="checkbox" id="chk_dabang" checked> <span class="badge badge-dabang">다방</span> <span id="cnt_dabang">0</span>건</label>
    <label><input type="checkbox" id="chk_daangn" checked> <span class="badge badge-daangn">당근</span> <span id="cnt_daangn">0</span>건</label>
    <label><input type="checkbox" id="chk_zigbang" checked> <span class="badge badge-zigbang">직방</span> <span id="cnt_zigbang">0</span>건</label>
    <label><input type="checkbox" id="chk_naver" checked> <span class="badge badge-naver">네이버</span> <span id="cnt_naver">0</span>건</label>
  </div>
  <div class="price-row" style="margin-top:10px">
    <label>보증금 최대 (만원)
      <input type="number" id="maxDeposit" value="3000" min="0" max="10000" step="100">
    </label>
    <label>월세 최대 (만원)
      <input type="number" id="maxRent" value="60" min="0" max="200" step="5">
    </label>
  </div>
  <div class="stats">
    <div class="stat-row"><span>표시 중</span><span id="totalCount" style="font-weight:700">0</span>건</div>
  </div>
  <div class="note-warn" style="font-size:12px;margin-top:8px">⚠️ 네이버 데이터는 분당 지역 포함</div>
  <div class="source-links">
    <a href="dabang.html"><span class="badge badge-dabang">다방</span> 목록 보기</a>
    <a href="daangn.html"><span class="badge badge-daangn">당근</span> 목록 보기</a>
    <a href="zigbang.html"><span class="badge badge-zigbang">직방</span> 목록 보기</a>
    <a href="naver.html"><span class="badge badge-naver">네이버</span> 목록 보기</a>
  </div>
</div>
<div class="map-full" id="map"></div>
<script>
$SHARED_JS
$allJs

const COLORS = {dabang:'#FF5C38', daangn:'#FF6F00', zigbang:'#6366F1', naver:'#03C75A'};
const NAMES  = {dabang:'다방', daangn:'당근', zigbang:'직방', naver:'네이버'};

const map = L.map('map').setView([37.2769, 127.0435], 15);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '&copy; OpenStreetMap contributors', maxZoom:19
}).addTo(map);

// Add Ajou University marker
L.marker([37.2785, 127.0434]).addTo(map)
  .bindPopup('<b>아주대학교 정문</b>').openPopup();

const allMarkers = [];

Object.entries(ALL_DATA).forEach(([src, rows]) => {
  rows.forEach(r => {
    if (!r.lat || !r.lon) return;
    const m = L.circleMarker([r.lat, r.lon], {
      radius:6, fillColor:COLORS[src], color:'#fff', weight:1.5, fillOpacity:0.85
    }).addTo(map);

    const img = r.img1 ? `<img class="popup-img" src="${r.img1}" onerror="this.style.display='none'">` : '';
    m.bindPopup(`<div class="popup-title"><span class="badge badge-${src}">${NAMES[src]}</span> ${r.title||''}</div>
${img}
<div class="popup-price">보증금 ${fmtDeposit(r.deposit)} / 월세 ${fmtRent(r.rent)}</div>
<div>관리비 ${fmtRent(r.maint)} | 총 <b>${fmtRent(r.total)}</b></div>
<div class="agency">${r.agency||''} ${r.phone||''}</div>
<div>${r.address||r.region||''}</div>
<div style="margin-top:6px"><a href="${r.url}" target="_blank" class="link-btn">매물 보기</a>
<a href="${src}.html" class="link-btn" style="margin-left:4px">${NAMES[src]} 목록</a></div>`);

    allMarkers.push({marker:m, src, data:r});
  });
});

function applyFilter() {
  const maxDep  = parseFloat(document.getElementById('maxDeposit').value)||9999;
  const maxRent = parseFloat(document.getElementById('maxRent').value)||999;
  const shown   = {dabang:0, daangn:0, zigbang:0, naver:0};

  allMarkers.forEach(({marker, src, data}) => {
    const chk = document.getElementById('chk_'+src).checked;
    const depOk  = data.deposit === null || data.deposit <= maxDep;
    const rentOk = data.rent    === null || data.rent    <= maxRent;
    if (chk && depOk && rentOk) {
      marker.setStyle({fillOpacity:0.85, opacity:1});
      shown[src]++;
    } else {
      marker.setStyle({fillOpacity:0, opacity:0});
    }
  });

  Object.keys(shown).forEach(src => {
    document.getElementById('cnt_'+src).textContent = shown[src];
  });
  document.getElementById('totalCount').textContent =
    Object.values(shown).reduce((a,b)=>a+b,0);
}

['chk_dabang','chk_daangn','chk_zigbang','chk_naver','maxDeposit','maxRent'].forEach(id => {
  document.getElementById(id).addEventListener('change', applyFilter);
  document.getElementById(id).addEventListener('input',  applyFilter);
});

applyFilter();
</script>
</body>
</html>
"@

[System.IO.File]::WriteAllText("$OutDir\index.html", $indexHtml, [System.Text.Encoding]::UTF8)
Write-Host "Wrote $OutDir\index.html"
Write-Host "Done. Open web\index.html in a browser."
