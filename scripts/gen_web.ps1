# gen_web.ps1 - Generates web/ HTML pages from the four platform CSVs.
# Reads Korean UI from template files (_tpl_platform.html, _tpl_index.html)
# so this script itself stays ASCII-safe and has no encoding issues on PS 5.1.
#
# Usage (run from workspace root):
#   powershell -ExecutionPolicy Bypass -File .\scripts\gen_web.ps1

param(
    [string]$DataDir = ".\data",
    [string]$OutDir  = ".\web"
)

$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$enc = [System.Text.Encoding]::UTF8

# ---------------------------------------------------------------------------
# CSV -> normalised JS-object helpers
# ---------------------------------------------------------------------------

function Esc($v) {
    if ($null -eq $v) { return "" }
    return "$v" `
        -replace '\\', '\\' `
        -replace '"', '\"' `
        -replace "`r`n", '\n' `
        -replace "`n", '\n' `
        -replace "`r", '\n'
}

function ToNum($v) {
    if ([string]::IsNullOrWhiteSpace($v)) { return "null" }
    $d = 0.0
    if ([double]::TryParse($v, [ref]$d)) { return "$d" }
    return "null"
}

function ToJsObj($h) {
    $parts = @()
    foreach ($kv in $h.GetEnumerator()) {
        $k = $kv.Key
        $v = $kv.Value
        if ($v -eq "null" -or ($v -ne "" -and $v -match '^-?[0-9]+(\.[0-9]+)?$')) {
            $parts += "`"$k`":$v"
        } else {
            $parts += "`"$k`":`"$(Esc $v)`""
        }
    }
    return "{" + ($parts -join ",") + "}"
}

function NormalDabang($r) {
    return ToJsObj @{
        source  = "dabang"
        id      = $r.listing_no
        url     = $r.url
        agency  = $r.agency
        phone   = $r.agent_phone
        region  = $r.region
        address = $r.address
        lat     = (ToNum $r.latitude)
        lon     = (ToNum $r.longitude)
        title   = $r.title
        deposit = (ToNum $r.deposit_manwon)
        rent    = (ToNum $r.rent_manwon)
        maint   = (ToNum $r.maintenance_manwon)
        total   = (ToNum $r.total_monthly_manwon)
        type    = $r.room_type
        area    = $r.area_m2
        floor   = $r.floor
        img1    = $r.image_1
        img2    = $r.image_2
    }
}

function NormalDaangn($r) {
    # Use ASCII codes DIRECT/BROKER; JS template translates to Korean display labels
    $agency = if ($r.writer_type -eq "DIRECT_USER") { "DIRECT" } else { "BROKER" }
    $region = @($r.region_depth2, $r.region_depth3) | Where-Object { $_ -ne "" } | Select-Object -First 2
    return ToJsObj @{
        source  = "daangn"
        id      = $r.listing_no
        url     = $r.url
        agency  = $agency
        phone   = ""
        region  = ($region -join " ")
        address = $r.address
        lat     = (ToNum $r.latitude)
        lon     = (ToNum $r.longitude)
        title   = $r.title
        deposit = (ToNum $r.deposit_manwon)
        rent    = (ToNum $r.rent_manwon)
        maint   = (ToNum $r.maintenance_manwon)
        total   = (ToNum $r.total_monthly_manwon)
        type    = $r.room_type
        area    = $r.area_m2
        floor   = $r.floor
        img1    = $r.image_1
        img2    = $r.image_2
    }
}

function NormalZigbang($r) {
    return ToJsObj @{
        source  = "zigbang"
        id      = $r.listing_no
        url     = $r.url
        agency  = $r.agency
        phone   = $r.agent_phone
        region  = $r.region
        address = $r.address
        lat     = (ToNum $r.latitude)
        lon     = (ToNum $r.longitude)
        title   = $r.title
        deposit = (ToNum $r.deposit_manwon)
        rent    = (ToNum $r.rent_manwon)
        maint   = (ToNum $r.maintenance_manwon)
        total   = (ToNum $r.total_monthly_manwon)
        type    = $r.room_type
        area    = $r.area_m2
        floor   = $r.floor
        img1    = $r.image_1
        img2    = $r.image_2
    }
}

function NormalNaver($r) {
    return ToJsObj @{
        source  = "naver"
        id      = $r.listing_no
        url     = $r.url
        agency  = $r.agency
        phone   = $r.agent_phone
        region  = $r.region
        address = $r.address
        lat     = (ToNum $r.latitude)
        lon     = (ToNum $r.longitude)
        title   = $r.title
        deposit = (ToNum $r.deposit_manwon)
        rent    = (ToNum $r.rent_manwon)
        maint   = (ToNum $r.maintenance_manwon)
        total   = (ToNum $r.total_monthly_manwon)
        type    = $r.room_type
        area    = $r.area_m2
        floor   = $r.floor
        img1    = $r.image_1
        img2    = $r.image_2
    }
}

function ToJsArray($rows, $normFn) {
    $objs = $rows | ForEach-Object { & $normFn $_ }
    return "[\n" + ($objs -join ",`n") + "`n]"
}

# ---------------------------------------------------------------------------
# Load CSVs
# ---------------------------------------------------------------------------
$dabang  = Import-Csv "$DataDir\dabang_ajou_2026-05-22.csv"     -Encoding UTF8
$daangn  = Import-Csv "$DataDir\daangn_ajou_2026-05-22.csv"     -Encoding UTF8
$zigbang = Import-Csv "$DataDir\zigbang_ajou_2026-05-22.csv"    -Encoding UTF8
$naver   = Import-Csv "$DataDir\naver_land_ajou_2026-05-22.csv" -Encoding UTF8

Write-Host "Loaded: dabang=$($dabang.Count) daangn=$($daangn.Count) zigbang=$($zigbang.Count) naver=$($naver.Count)"

$jsDabang  = ToJsArray $dabang  ${function:NormalDabang}
$jsDaangn  = ToJsArray $daangn  ${function:NormalDaangn}
$jsZigbang = ToJsArray $zigbang ${function:NormalZigbang}
$jsNaver   = ToJsArray $naver   ${function:NormalNaver}

# ---------------------------------------------------------------------------
# Read templates (written by Write tool as UTF-8, read correctly here)
# ---------------------------------------------------------------------------
$tplPlatform = [System.IO.File]::ReadAllText("$PSScriptRoot\_tpl_platform.html", $enc)
$tplIndex    = [System.IO.File]::ReadAllText("$PSScriptRoot\_tpl_index.html",    $enc)

function Write-Platform($file, $source, $accent, $data, $note) {
    $html = $tplPlatform
    $html = $html.Replace('__SOURCE__',     $source)
    $html = $html.Replace('__ACCENT__',     $accent)
    $html = $html.Replace('__EXTRA_NOTE__', $note)
    $html = $html.Replace('__DATA__',       $data)
    [System.IO.File]::WriteAllText("$OutDir\$file", $html, $enc)
    Write-Host "Wrote $OutDir\$file"
}

# ---------------------------------------------------------------------------
# Per-platform pages
# ---------------------------------------------------------------------------
Write-Platform "dabang.html"  "dabang"  "#FF5C38" $jsDabang  ""
Write-Platform "daangn.html"  "daangn"  "#FF6F00" $jsDaangn  ""
Write-Platform "zigbang.html" "zigbang" "#6366F1" $jsZigbang ""
Write-Platform "naver.html"   "naver"   "#03C75A" $jsNaver   ""

# ---------------------------------------------------------------------------
# Combined map page (index.html)
# ---------------------------------------------------------------------------
$allJs = "const ALL_DATA = {`n  dabang:`n"  + $jsDabang  + ",`n" +
                            "  daangn:`n"  + $jsDaangn  + ",`n" +
                            "  zigbang:`n" + $jsZigbang + ",`n" +
                            "  naver:`n"   + $jsNaver   + "`n};"

$indexHtml = $tplIndex.Replace('__ALL_JS__', $allJs)
[System.IO.File]::WriteAllText("$OutDir\index.html", $indexHtml, $enc)
Write-Host "Wrote $OutDir\index.html"
Write-Host "Done. Open web\index.html in a browser (port 3000 via static server)."
