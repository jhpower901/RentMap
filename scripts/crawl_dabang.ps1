param(
    [double]$MinLat = 37.2736,
    [double]$MinLng = 127.0408,
    [double]$MaxLat = 37.2809,
    [double]$MaxLng = 127.0494,
    [int]$Zoom = 18,
    [int]$MaxDeposit = 3000,
    [int]$MaxRent = 60,
    [string]$OutputCsv = ".\data\dabang_ajou_2026-05-22.csv",
    [string]$RawJson = ""
)

$ErrorActionPreference = "Stop"

function Get-FirstValue {
    param(
        [object]$Object,
        [string[]]$Names
    )

    if ($null -eq $Object) {
        return $null
    }

    foreach ($name in $Names) {
        if ($Object.PSObject.Properties.Name -contains $name) {
            $value = $Object.$name
            if ($null -ne $value -and "$value" -ne "") {
                return $value
            }
        }
    }

    return $null
}

function Get-NestedValue {
    param(
        [object]$Object,
        [string[][]]$Paths
    )

    foreach ($path in $Paths) {
        $current = $Object
        foreach ($part in $path) {
            if ($null -eq $current -or -not ($current.PSObject.Properties.Name -contains $part)) {
                $current = $null
                break
            }
            $current = $current.$part
        }

        if ($null -ne $current -and "$current" -ne "") {
            return $current
        }
    }

    return $null
}

function Join-TextList {
    param([object]$Value)

    if ($null -eq $Value) {
        return ""
    }

    if ($Value -is [string]) {
        return $Value
    }

    $items = @()
    foreach ($item in @($Value)) {
        if ($null -eq $item) {
            continue
        }

        if ($item -is [string]) {
            $items += $item
            continue
        }

        $label = Get-FirstValue $item @("name", "title", "label", "option_name", "optionName", "value")
        if ($null -ne $label) {
            $items += "$label"
        }
    }

    return ($items | Where-Object { $_ -ne "" } | Select-Object -Unique) -join "; "
}

function Get-ImageUrl {
    param(
        [object]$Images,
        [int]$Index
    )

    if ($null -eq $Images) {
        return ""
    }

    $arr = @($Images)
    if ($arr.Count -le $Index) {
        return ""
    }

    $image = $arr[$Index]
    if ($image -is [string]) {
        return $image
    }

    if (($image.PSObject.Properties.Name -contains "prefix_url") -and ($image.PSObject.Properties.Name -contains "id")) {
        return "$($image.prefix_url)$($image.id)"
    }

    $url = Get-FirstValue $image @("url", "image_url", "imageUrl", "src", "origin", "large", "medium")
    if ($null -ne $url) {
        return "$url"
    }

    return ""
}

function Convert-ToManwonNumber {
    param([object]$Value)

    if ($null -eq $Value -or "$Value" -eq "") {
        return $null
    }

    $text = "$Value"
    $text = $text -replace "[^0-9.]", ""
    if ($text -eq "") {
        return $null
    }

    return [double]$text
}

$filters = @{
    sellingTypeList = @("MONTHLY_RENT")
    depositRange = @{ min = 0; max = $MaxDeposit }
    priceRange = @{ min = 0; max = $MaxRent }
    isIncludeMaintenance = $false
    pyeongRange = @{ min = 0; max = 999999 }
    useApprovalDateRange = @{ min = 0; max = 999999 }
    roomFloorList = @("GROUND_FIRST", "GROUND_SECOND_OVER", "SEMI_BASEMENT", "ROOFTOP")
    roomTypeList = @("ONE_ROOM", "TWO_ROOM")
    dealTypeList = @("AGENT")
    canParking = $false
    isShortLease = $false
    hasElevator = $false
    hasPano = $false
    isDivision = $false
    isDuplex = $false
}

$locationJson = "[{""sw"":{""lat"":$MinLat,""lng"":$MinLng},""ne"":{""lat"":$MaxLat,""lng"":$MaxLng}}]"

$headers = @{
    "Accept" = "application/json, text/plain, */*"
    "D-Api-Version" = "5.0.0"
    "D-App-Version" = "1"
    "D-Call-Type" = "web"
    "csrf" = "token"
    "Referer" = "https://www.dabangapp.com/map/onetwo"
    "User-Agent" = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
    "Cache-Control" = "no-cache"
    "Pragma" = "no-cache"
    "Content-Type" = "application/json"
    "Origin" = "https://www.dabangapp.com"
    "Sec-Fetch-Site" = "same-origin"
    "Sec-Fetch-Mode" = "cors"
    "Sec-Fetch-Dest" = "empty"
}

$encodedFilters = [System.Uri]::EscapeDataString(($filters | ConvertTo-Json -Depth 10 -Compress))
$bboxJson = "{""sw"":{""lat"":$MinLat,""lng"":$MinLng},""ne"":{""lat"":$MaxLat,""lng"":$MaxLng}}"
$encodedBbox = [System.Uri]::EscapeDataString($bboxJson)

Write-Host "Fetching list..."
$rooms = @()
$page = 1
do {
    $listUrl = "https://www.dabangapp.com/api/v5/room-list/category/one-two/bbox?filters=$encodedFilters&bbox=$encodedBbox&zoom=$Zoom&useMap=naver&page=$page"
    $listResponse = Invoke-RestMethod -Method Get -Uri $listUrl -Headers $headers

    $result = if ($listResponse.PSObject.Properties.Name -contains "result") { $listResponse.result } else { $listResponse }
    $pageRooms = @($result.roomList)
    $rooms += $pageRooms
    $hasMore = [bool]$result.hasMore
    $page++
} while ($hasMore)

if ($rooms.Count -eq 0) {
    throw "No rooms found. Inspect the list API response shape."
}

Write-Host "Found $($rooms.Count) list rows. Fetching details..."

$detailHeaders = $headers.Clone()
$detailHeaders["D-Api-Version"] = "3.0.1"

$records = @()
$rawDetails = @()
$seen = @{}

foreach ($room in $rooms) {
    $roomId = Get-FirstValue $room @("id", "room_id", "roomId", "seq", "hash")
    if ($null -eq $roomId) {
        continue
    }

    $roomId = "$roomId"
    if ($seen.ContainsKey($roomId)) {
        continue
    }
    $seen[$roomId] = $true

    $detailUrl = "https://www.dabangapp.com/api/3/new-room/detail?room_id=$([System.Uri]::EscapeDataString($roomId))&api_version=3.0.1&call_type=web&version=1"

    try {
        $detailResponse = Invoke-RestMethod -Method Get -Uri $detailUrl -Headers $detailHeaders
    }
    catch {
        Write-Warning "Detail fetch failed for room_id=${roomId}: $($_.Exception.Message)"
        continue
    }

    $detail = if ($detailResponse.PSObject.Properties.Name -contains "result") { $detailResponse.result } else { $detailResponse }

    $rawDetails += $detail

    $roomData = Get-FirstValue $detail @("room")
    if ($null -eq $roomData) {
        $roomData = $detail
    }

    $agent = Get-FirstValue $detail @("agent", "agency", "agent_info", "agentInfo", "office")
    $region = Get-FirstValue $detail @("region")

    $listingNo = Get-FirstValue $roomData @("seq", "room_seq", "roomSeq", "room_no", "roomNo", "id")
    $publicRoomId = Get-FirstValue $roomData @("id", "room_id", "roomId")
    if ($null -eq $publicRoomId) {
        $publicRoomId = $roomId
    }

    $priceTitle = Get-FirstValue $roomData @("price_title", "priceTitle")
    $deposit = $null
    $rent = $null
    if ($priceTitle -match "([0-9,]+)\s*/\s*([0-9,]+)") {
        $deposit = Convert-ToManwonNumber $Matches[1]
        $rent = Convert-ToManwonNumber $Matches[2]
    }

    $maintenanceWon = Convert-ToManwonNumber (Get-FirstValue $roomData @("maintenance_cost", "maintenanceCost"))
    $maintenance = $null
    if ($null -ne $maintenanceWon) {
        $maintenance = [math]::Round($maintenanceWon / 10000, 1)
    }
    if ($null -eq $maintenance) {
        $maintenance = Convert-ToManwonNumber (Get-FirstValue $roomData @("maintenance_cost_str", "maintenanceCostStr"))
    }
    if ($null -eq $maintenance) {
        $maintenance = 0
    }

    $images = Get-FirstValue $detail @("image_list", "imageList", "images", "photos", "room_images", "roomImages")
    $options = Get-FirstValue $roomData @("room_options", "roomOptions", "options", "option")
    $securityOptions = Get-FirstValue $roomData @("safeties", "safety_options", "safetyOptions", "security_options", "securityOptions")
    $location = Get-FirstValue $roomData @("location")
    $lng = ""
    $lat = ""
    if ($null -ne $location) {
        $locationArray = @($location)
        if ($locationArray.Count -ge 2) {
            $lng = "$($locationArray[0])"
            $lat = "$($locationArray[1])"
        }
    }
    $isShowDetailAddress = Get-FirstValue $roomData @("is_show_detail_address", "isShowDetailAddress")
    $addressPublicLevel = if ($isShowDetailAddress -eq $true) { "exact_address_visible" } else { "dong_only_ask_agency_for_exact_jibun" }

    $records += [pscustomobject]@{
        source = "dabang"
        listing_no = "$listingNo"
        room_id = "$publicRoomId"
        url = "https://www.dabangapp.com/room/$publicRoomId"
        agency = "$(Get-FirstValue $agent @("name", "office_name", "officeName", "agent_name", "agentName"))"
        agent_name = "$(Get-FirstValue $agent @("facename", "representative_name", "representativeName", "owner_name", "ownerName"))"
        agent_phone = "$(Get-FirstValue $agent @("agent_tel", "phone", "tel", "telephone", "cell_phone", "cellPhone"))"
        region = "$(Get-FirstValue $region @("full_name", "name"))"
        address = "$(Get-FirstValue $roomData @("full_jibun_address_str", "full_road_address_str", "address"))"
        latitude = $lat
        longitude = $lng
        address_public_level = $addressPublicLevel
        title = "$(Get-FirstValue $roomData @("title", "name", "description_title", "descriptionTitle"))"
        deposit_manwon = $deposit
        rent_manwon = $rent
        maintenance_manwon = $maintenance
        total_monthly_manwon = $(if ($null -ne $rent) { [math]::Round(($rent + $maintenance), 1) } else { $null })
        room_type = "$(Get-FirstValue $roomData @("room_type_str", "roomTypeStr", "room_type_main_str", "roomTypeMainStr"))"
        area_m2 = "$(Get-FirstValue $roomData @("room_size", "roomSize", "provision_size", "provisionSize"))"
        floor = "$(Get-FirstValue $roomData @("room_floor_str", "roomFloorStr"))/$(Get-FirstValue $roomData @("building_floor_str", "buildingFloorStr"))"
        direction = "$(Get-FirstValue $roomData @("direction_str", "directionStr", "direction"))"
        parking = "$(Get-FirstValue $roomData @("parking_str", "parkingStr", "parking"))"
        move_in = "$(Get-FirstValue $roomData @("moving_date", "movingDate"))"
        approval_date = "$(Get-FirstValue $roomData @("building_approval_date_str", "buildingApprovalDateStr"))"
        building_use = "$(Join-TextList (Get-FirstValue $roomData @("building_use_types_str", "buildingUseTypesStr")))"
        options = "$(Join-TextList $options)"
        security_options = "$(Join-TextList $securityOptions)"
        image_1 = "$(Get-ImageUrl $images 0)"
        image_2 = "$(Get-ImageUrl $images 1)"
        crawl_note = ""
    }

    Start-Sleep -Milliseconds 120
}

$outDir = Split-Path -Parent $OutputCsv
if ($outDir -and -not (Test-Path $outDir)) {
    New-Item -ItemType Directory -Force -Path $outDir | Out-Null
}

$records |
    Sort-Object @{ Expression = "agency"; Ascending = $true }, @{ Expression = "total_monthly_manwon"; Ascending = $true }, @{ Expression = "rent_manwon"; Ascending = $true } |
    Export-Csv -Path $OutputCsv -NoTypeInformation -Encoding UTF8

if ($RawJson -ne "") {
    $rawDir = Split-Path -Parent $RawJson
    if ($rawDir -and -not (Test-Path $rawDir)) {
        New-Item -ItemType Directory -Force -Path $rawDir | Out-Null
    }

    $rawDetails | ConvertTo-Json -Depth 20 | Set-Content -Path $RawJson -Encoding UTF8
    Write-Host "Wrote raw details to $RawJson"
}

Write-Host "Wrote $($records.Count) rows to $OutputCsv"
