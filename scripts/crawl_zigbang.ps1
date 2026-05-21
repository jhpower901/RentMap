param(
    [string]$OutputCsv = ".\data\zigbang_ajou_2026-05-22.csv",
    [string[]]$Geohashes = @("wydk4", "wydk5"),
    [double]$MinLat = 37.2736,
    [double]$MinLng = 127.0408,
    [double]$MaxLat = 37.2809,
    [double]$MaxLng = 127.0494,
    [int]$MaxDepositManwon = 3000,
    [int]$MaxRentManwon = 60
)

$ErrorActionPreference = "Stop"

function Join-Values($Value) {
    if ($null -eq $Value) { return "" }
    if ($Value -is [System.Array]) {
        return (($Value | ForEach-Object {
            if ($null -eq $_) { "" }
            elseif ($_.PSObject.Properties["name"]) { $_.name }
            elseif ($_.PSObject.Properties["label"]) { $_.label }
            else { "$_" }
        }) | Where-Object { $_ -ne "" }) -join "; "
    }
    return "$Value"
}

function Get-Prop($Object, [string]$Name) {
    if ($null -eq $Object) { return $null }
    $prop = $Object.PSObject.Properties[$Name]
    if ($null -eq $prop) { return $null }
    return $prop.Value
}

function Normalize-Phone($Phone) {
    if ([string]::IsNullOrWhiteSpace($Phone)) { return "" }
    $digits = ($Phone -replace "[^0-9]", "")
    if ($digits.Length -eq 8 -and $digits.StartsWith("02")) {
        return "{0}-{1}-{2}" -f $digits.Substring(0,2), $digits.Substring(2,3), $digits.Substring(5)
    }
    if ($digits.Length -eq 9 -and $digits.StartsWith("02")) {
        return "{0}-{1}-{2}" -f $digits.Substring(0,2), $digits.Substring(2,3), $digits.Substring(5)
    }
    if ($digits.Length -eq 10 -and $digits.StartsWith("02")) {
        return "{0}-{1}-{2}" -f $digits.Substring(0,2), $digits.Substring(2,4), $digits.Substring(6)
    }
    if ($digits.Length -eq 10) {
        return "{0}-{1}-{2}" -f $digits.Substring(0,3), $digits.Substring(3,3), $digits.Substring(6)
    }
    if ($digits.Length -eq 11) {
        return "{0}-{1}-{2}" -f $digits.Substring(0,3), $digits.Substring(3,4), $digits.Substring(7)
    }
    return $Phone
}

function Format-DateText($Value) {
    if ([string]::IsNullOrWhiteSpace($Value)) { return "" }
    if ($Value -match "^\d{8}$") {
        return "{0}.{1}.{2}" -f $Value.Substring(0,4), $Value.Substring(4,2), $Value.Substring(6,2)
    }
    return "$Value"
}

function Get-FloorText($FloorObject) {
    if ($null -eq $FloorObject) { return "" }
    $floor = Get-Prop $FloorObject "floor"
    $allFloors = Get-Prop $FloorObject "allFloors"
    if ($null -ne $floor -and $null -ne $allFloors) { return "$floor/$allFloors" }
    if ($null -ne $floor) { return "$floor" }
    return ""
}

function Get-AreaM2($AreaObject) {
    if ($null -eq $AreaObject) { return "" }
    foreach ($prop in $AreaObject.PSObject.Properties) {
        if ($prop.Name -like "*M2" -and $null -ne $prop.Value) {
            return $prop.Value
        }
    }
    return ""
}

$headers = @{
    "User-Agent" = "Mozilla/5.0"
    "Accept" = "application/json, text/plain, */*"
    "Origin" = "https://www.zigbang.com"
    "Referer" = "https://www.zigbang.com/"
}

$itemsById = @{}
foreach ($geohash in $Geohashes) {
    $listUrl = "https://apis.zigbang.com/v2/items/oneroom?geohash=$geohash&depositMin=0&rentMin=0&salesTypes%5B0%5D=%EC%9B%94%EC%84%B8&domain=zigbang&checkAnyItemWithoutFilter=true"
    Write-Host "Fetching list $geohash"
    $list = Invoke-RestMethod -Uri $listUrl -Headers $headers -Method Get
    foreach ($item in $list.items) {
        $lat = [double]$item.lat
        $lng = [double]$item.lng
        if ($lat -lt $MinLat -or $lat -gt $MaxLat -or $lng -lt $MinLng -or $lng -gt $MaxLng) { continue }
        $itemsById["$($item.itemId)"] = $item
    }
}

Write-Host ("Detail candidates in bbox: {0}" -f $itemsById.Count)

$rows = New-Object System.Collections.Generic.List[object]
$i = 0
foreach ($id in ($itemsById.Keys | Sort-Object)) {
    $i++
    if ($i % 20 -eq 0) { Write-Host ("Fetched details: {0}/{1}" -f $i, $itemsById.Count) }
    $detailUrl = "https://apis.zigbang.com/v3/items/$id"
    try {
        $detail = Invoke-RestMethod -Uri $detailUrl -Headers $headers -Method Get
        $item = $detail.item
        if ($null -eq $item) { continue }

        $deposit = [int](Get-Prop $item.price "deposit")
        $rent = [int](Get-Prop $item.price "rent")
        if ($rent -le 0) { continue }
        if ($deposit -gt $MaxDepositManwon -or $rent -gt $MaxRentManwon) { continue }

        $manageCost = Get-Prop $item.manageCost "amount"
        $totalMonthly = ""
        if ($null -ne $manageCost) { $totalMonthly = [int]$rent + [int]$manageCost }

        $addressOrigin = $item.addressOrigin
        $location = $item.location
        $images = @($item.images)

        $rows.Add([pscustomobject]@{
            source = "zigbang"
            listing_no = "$($item.itemId)"
            item_id = "$($item.itemId)"
            url = "https://www.zigbang.com/home/oneroom/items/$($item.itemId)?itemDetailType=ZIGBANG"
            agency = "$($detail.agent.agentTitle)"
            agent_name = "$($detail.agent.agentName)"
            agent_phone = Normalize-Phone $detail.agent.agentPhone
            realtor_name = "$($detail.realtor.name)"
            realtor_phone = Normalize-Phone $detail.realtor.phone
            agency_address = "$($detail.agent.agentAddress)"
            agency_reg_no = "$($detail.realtor.officeRegNumber)"
            region = "$($addressOrigin.fullText)"
            address = "$($item.jibunAddress)"
            latitude = Get-Prop $location "lat"
            longitude = Get-Prop $location "lng"
            address_public_level = "exact_jibun_from_api"
            title = "$($item.title)"
            deposit_manwon = $deposit
            rent_manwon = $rent
            maintenance_manwon = $manageCost
            total_monthly_manwon = $totalMonthly
            room_type = "$($item.roomType)"
            service_type = "$($item.serviceType)"
            area_m2 = Get-AreaM2 $item.area
            floor = Get-FloorText $item.floor
            direction = "$($item.roomDirection)"
            parking = "$($item.parkingAvailableText)"
            move_in = "$($item.moveinDate)"
            approval_date = Format-DateText $item.approveDate
            residence_type = "$($item.residenceType)"
            non_compliant_building = "$($item.nonCompliantBuilding)"
            options = Join-Values $item.options
            image_1 = if ($images.Count -gt 0) { $images[0] } else { "" }
            image_2 = if ($images.Count -gt 1) { $images[1] } else { "" }
            crawl_note = ""
        }) | Out-Null
    }
    catch {
        Write-Warning "Failed detail $id`: $($_.Exception.Message)"
    }
}

$outDir = Split-Path -Parent $OutputCsv
if ($outDir) { New-Item -ItemType Directory -Force -Path $outDir | Out-Null }
$rows |
    Sort-Object @{ Expression = "agency"; Ascending = $true }, @{ Expression = "rent_manwon"; Ascending = $true }, @{ Expression = "deposit_manwon"; Ascending = $true } |
    Export-Csv -Path $OutputCsv -NoTypeInformation -Encoding UTF8

Write-Host ("Wrote {0} rows to {1}" -f $rows.Count, $OutputCsv)
