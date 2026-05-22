param(
    [int[]]$RegionIds = @(1289, 1290, 1298, 1294, 1295, 1296, 1297, 1291),
    [int]$MaxDeposit = 3000,
    [int]$MaxRent = 60,
    [string]$OutputCsv = ".\data\daangn_ajou_2026-05-22.csv",
    [switch]$SkipDetail,
    # Optional bounding box filter (applied after detail fetch using publicCoordinate).
    # Set both Min* and Max* to activate; 0/0 means no filter.
    [double]$MinLat = 0,
    [double]$MaxLat = 0,
    [double]$MinLng = 0,
    [double]$MaxLng = 0
)

# RegionIds default covers Ajou University area dongs:
#   1289=우만1동, 1290=우만2동 (main gate area, 수원시 팔달구)
#   1298=원천동 (east of campus, 수원시 영통구)
#   1294-1297=매탄1-4동 (south of campus, 수원시 영통구)
#   1291=인계동 (west, 수원시 팔달구)

$ErrorActionPreference = "Stop"

$ValidSalesTypes = @('SPLIT_ONE_ROOM', 'OPEN_ONE_ROOM', 'TWO_ROOM', 'OFFICETEL')

function Invoke-GetUtf8 {
    param([string]$Url, [int]$DelayMs = 0)
    $req = [System.Net.HttpWebRequest]::Create($Url)
    $req.Timeout = 15000
    $req.ReadWriteTimeout = 15000
    $req.UserAgent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
    $req.Accept = "text/html,application/xhtml+xml"
    $req.Headers["Accept-Language"] = "ko-KR,ko;q=0.9"
    $resp = $req.GetResponse()
    $stream = $resp.GetResponseStream()
    $reader = New-Object System.IO.StreamReader($stream, [System.Text.Encoding]::UTF8)
    $html = $reader.ReadToEnd()
    $reader.Close()
    $resp.Close()
    if ($DelayMs -gt 0) { Start-Sleep -Milliseconds $DelayMs }
    return $html
}

function Get-ListingsFromRegion {
    param([int]$RegionId)

    $url = "https://www.daangn.com/kr/realty/?in=x-$RegionId"
    try {
        $html = Invoke-GetUtf8 -Url $url
    } catch {
        Write-Warning "Region $RegionId fetch failed: $($_.Exception.Message)"
        return @()
    }

    $ctxMarker = 'window.__remixContext = '
    $ctxStart = $html.IndexOf($ctxMarker)
    if ($ctxStart -lt 0) { return @() }
    $ctxStart += $ctxMarker.Length

    $scriptEnd = $html.IndexOf('</script>', $ctxStart)
    if ($scriptEnd -lt 0) { return @() }

    $ctxJson = $html.Substring($ctxStart, $scriptEnd - $ctxStart).TrimEnd(';', ' ', "`n", "`r")

    try {
        $ctx = $ctxJson | ConvertFrom-Json
    } catch {
        Write-Warning "Region $RegionId JSON parse failed: $($_.Exception.Message)"
        return @()
    }

    $routeData = $ctx.state.loaderData.'routes/kr.realty._index'
    if ($null -eq $routeData) { return @() }

    $searchRegion = $routeData.searchRegion
    $allListings = $routeData.realtyPosts.realtyPosts
    if ($null -eq $allListings) { return @() }

    $filtered = $allListings | Where-Object {
        $_.salesType -in $ValidSalesTypes -and
        $null -ne ($_.trades | Where-Object {
            $_.type -eq 'MONTH' -and
            $_.deposit -le $MaxDeposit -and
            $_.monthlyPay -le $MaxRent
        } | Select-Object -First 1)
    }

    foreach ($listing in $filtered) {
        $listing | Add-Member -NotePropertyName '_regionInfo' -NotePropertyValue $searchRegion -Force
    }

    return @($filtered)
}

function Get-ArticleRelayStore {
    param([string]$ArticleId)

    $url = "https://realty.daangn.com/articles/$ArticleId"
    try {
        $html = Invoke-GetUtf8 -Url $url -DelayMs 150
    } catch {
        Write-Warning "Article $ArticleId fetch failed: $($_.Exception.Message)"
        return $null
    }

    $marker = 'window.RELAY_STORE = "'
    $start = $html.IndexOf($marker)
    if ($start -lt 0) { return $null }
    $start += $marker.Length

    # Find end quote (unescaped - scan char by char)
    $end = -1
    $esc = $false
    for ($i = $start; $i -lt $html.Length; $i++) {
        if ($esc) { $esc = $false; continue }
        if ($html[$i] -eq '\') { $esc = $true; continue }
        if ($html[$i] -eq '"') { $end = $i; break }
    }
    if ($end -lt 0) { return $null }

    # The RELAY_STORE value is a JS-escaped JSON string.
    # Wrap in quotes and parse as JSON string to get the inner JSON, then parse again.
    $escapedStr = $html.Substring($start, $end - $start)
    try {
        $innerJson = ('"' + $escapedStr + '"') | ConvertFrom-Json
        $rs = $innerJson | ConvertFrom-Json
        return $rs
    } catch {
        return $null
    }
}

function Get-ArticleDetailFast {
    param([string]$ArticleId)

    $url = "https://realty.daangn.com/articles/$ArticleId"
    try {
        $html = Invoke-GetUtf8 -Url $url -DelayMs 80
    } catch {
        Write-Warning "Article $ArticleId fetch failed: $($_.Exception.Message)"
        return $null
    }

    $detail = [ordered]@{
        lat          = ""
        lon          = ""
        publicAddress = ""
        roomCnt      = ""
        approvalDate = ""
        writerType   = ""
    }

    $coordRefMatch = [regex]::Match($html, 'originalId\\":\\"' + [regex]::Escape($ArticleId) + '\\".*?publicCoordinate\\":\{\\"__ref\\":\\"([^\\"]+)')
    if ($coordRefMatch.Success) {
        $coordRef = [regex]::Escape($coordRefMatch.Groups[1].Value)
        $coordMatch = [regex]::Match($html, $coordRef + '\\":\{\\"__id\\":\\"[^\\"]+\\",\\"__typename\\":\\"Coordinate\\",\\"lat\\":\\"([^\\"]+)\\",\\"lon\\":\\"([^\\"]+)')
        if ($coordMatch.Success) {
            $detail.lat = $coordMatch.Groups[1].Value
            $detail.lon = $coordMatch.Groups[2].Value
        }
    }

    $fieldMap = @{
        publicAddress = 'publicAddress\\":\\"([^\\"]*)'
        roomCnt       = 'roomCnt\\":\\"?([^\\",}]*)'
        approvalDate  = 'buildingApprovalDate\\":\\"([^\\"]*)'
        writerType    = 'writerTypeV2\\":\\"([^\\"]*)'
    }
    foreach ($key in $fieldMap.Keys) {
        $match = [regex]::Match($html, $fieldMap[$key])
        if ($match.Success) {
            $detail[$key] = $match.Groups[1].Value
        }
    }

    return [pscustomobject]$detail
}

function Resolve-RelayRef {
    param([object]$Store, [object]$Value)
    if ($null -eq $Value) { return $null }
    if ($Value -is [pscustomobject] -and $Value.PSObject.Properties.Name -contains '__ref') {
        $ref = $Value.'__ref'
        if ($Store.PSObject.Properties.Name -contains $ref) {
            return $Store.$ref
        }
        return $null
    }
    return $Value
}

function Get-ImageUrl {
    param([object]$Images, [int]$Index)
    if ($null -eq $Images) { return "" }
    $arr = @($Images)
    if ($arr.Count -le $Index) { return "" }
    $img = $arr[$Index]
    if ($img -is [string]) { return $img }
    if ($img.PSObject.Properties.Name -contains 'url') { return "$($img.url)" }
    return ""
}

# Collect listings from each region
Write-Host "Fetching listings from $($RegionIds.Count) regions..."
$allRaw = @()
$seen = @{}

foreach ($regionId in $RegionIds) {
    $listings = Get-ListingsFromRegion -RegionId $regionId
    Write-Host "  Region ${regionId}: $($listings.Count) listings within budget"

    foreach ($l in $listings) {
        $articleId = [regex]::Match($l.webUrl, '/articles/(\d+)').Groups[1].Value
        if ($seen.ContainsKey($articleId)) { continue }
        $seen[$articleId] = $true
        $allRaw += $l
    }
}

Write-Host "Total unique listings: $($allRaw.Count)"

# Fetch detail pages and build records
$records = @()
$idx = 0

foreach ($l in $allRaw) {
    $idx++
    $articleId = [regex]::Match($l.webUrl, '/articles/(\d+)').Groups[1].Value
    Write-Host "[$idx/$($allRaw.Count)] $articleId"

    $trade = $l.trades | Where-Object { $_.type -eq 'MONTH' } | Select-Object -First 1
    $regionInfo = $l._regionInfo

    $lat = ""
    $lon = ""
    $publicAddr = ""
    $roomCnt = ""
    $approvalDate = ""
    $writerType = ""
    $detailManageCost = $null

    if (-not $SkipDetail) {
        $detail = Get-ArticleDetailFast -ArticleId $articleId
        if ($null -ne $detail) {
            $lat = "$($detail.lat)"
            $lon = "$($detail.lon)"
            $publicAddr = "$($detail.publicAddress)"
            $roomCnt = "$($detail.roomCnt)"
            $approvalDate = "$($detail.approvalDate)"
            $writerType = "$($detail.writerType)"
        }
    }

    # Fallbacks from list data
    if ($publicAddr -eq "" -and $null -ne $l.address) { $publicAddr = "$($l.address)" }
    if ($approvalDate -eq "" -and $null -ne $l.buildingApprovalDate) { $approvalDate = "$($l.buildingApprovalDate)" }
    if ($writerType -eq "" -and $null -ne $l.writerType) { $writerType = "$($l.writerType)" }

    # Maintenance: use listing-level manageCost (in 만원 already)
    $maintenanceWon = 0
    if ($null -ne $l.manageCost -and "$($l.manageCost)" -ne "") {
        $maintenanceWon = [double]$l.manageCost
    }

    $rentManwon = [double]$trade.monthlyPay
    $totalMonthly = [math]::Round($rentManwon + $maintenanceWon, 1)

    # Clean title: strip trailing " | 당근부동산"
    $title = "$($l.title)" -replace '\s*\|\s*[^\|]+$', ''

    $records += [pscustomobject]@{
        source               = "daangn"
        listing_no           = $articleId
        url                  = "https://realty.daangn.com/articles/$articleId"
        writer_type          = $writerType
        region_depth1        = if ($null -ne $regionInfo) { "$($regionInfo.depth1RegionName)" } else { "" }
        region_depth2        = if ($null -ne $regionInfo) { "$($regionInfo.depth2RegionName)" } else { "" }
        region_depth3        = if ($null -ne $regionInfo) { "$($regionInfo.depth3RegionName)" } else { "" }
        address              = $publicAddr
        latitude             = $lat
        longitude            = $lon
        title                = $title
        deposit_manwon       = [double]$trade.deposit
        rent_manwon          = $rentManwon
        maintenance_manwon   = $maintenanceWon
        total_monthly_manwon = $totalMonthly
        room_type            = "$($l.salesType)"
        room_count           = $roomCnt
        area_m2              = "$($l.area)"
        floor                = "$($l.floor)"
        approval_date        = $approvalDate
        image_1              = Get-ImageUrl $l.images 0
        image_2              = Get-ImageUrl $l.images 1
        crawl_note           = ""
    }
}

$outDir = Split-Path -Parent $OutputCsv
if ($outDir -and -not (Test-Path $outDir)) {
    New-Item -ItemType Directory -Force -Path $outDir | Out-Null
}

# Apply bounding box filter if all four bounds are provided
$useBbox = ($MinLat -ne 0 -and $MaxLat -ne 0 -and $MinLng -ne 0 -and $MaxLng -ne 0)
$filtered = if ($useBbox) {
    $records | Where-Object {
        $latOk = $_.latitude -eq "" -or ($_.latitude -ne "" -and [double]$_.latitude -ge $MinLat -and [double]$_.latitude -le $MaxLat)
        $lonOk = $_.longitude -eq "" -or ($_.longitude -ne "" -and [double]$_.longitude -ge $MinLng -and [double]$_.longitude -le $MaxLng)
        $latOk -and $lonOk
    }
} else { $records }

Write-Host "Bbox filter: $($records.Count) -> $(@($filtered).Count) records"

@($filtered) |
    Sort-Object @{ Expression = "region_depth3"; Ascending = $true },
                @{ Expression = "total_monthly_manwon"; Ascending = $true },
                @{ Expression = "rent_manwon"; Ascending = $true } |
    Export-Csv -Path $OutputCsv -NoTypeInformation -Encoding UTF8

Write-Host "Wrote $(@($filtered).Count) rows to $OutputCsv"
