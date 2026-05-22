param(
    [string]$Url = "https://new.land.naver.com/rooms?ms=2AzWj5,3zkqG6,17&a=APT:OPST:ABYG:OBYG:GM:OR:DDDGG:JWJT:SGJT:VL&e=RETAIL&aa=SMALLSPCRENT",
    [string]$OutputCsv = ".\data\naver_land_ajou_2026-05-22.csv",
    [string]$RawJson = "",
    [int]$MaxPages = 5,
    [string]$ChromePath = "",
    [switch]$Headed,
    [double]$MinLat = 37.273187,
    [double]$MaxLat = 37.282688,
    [double]$MinLng = 127.038562,
    [double]$MaxLng = 127.049312
)

$ErrorActionPreference = "Stop"

$runtimeRoot = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\node"
$node = Join-Path $runtimeRoot "bin\node.exe"
$nodeModules = Join-Path $runtimeRoot "node_modules"
$playwrightCoreModules = Join-Path $nodeModules ".pnpm\playwright-core@1.60.0\node_modules"

if (-not (Test-Path $node)) {
    throw "Bundled Node.js was not found at $node"
}

$env:NODE_PATH = "$nodeModules;$playwrightCoreModules"

$scriptPath = Join-Path $PSScriptRoot "crawl_naver_land.js"
$args = @(
    $scriptPath,
    "--url", $Url,
    "--output-csv", $OutputCsv,
    "--max-pages", "$MaxPages",
    "--min-lat", "$MinLat",
    "--max-lat", "$MaxLat",
    "--min-lng", "$MinLng",
    "--max-lng", "$MaxLng",
    "--skip-home"   # avoids Naver home page setting wrong cortarNo via IP geolocation
)

if ($RawJson -ne "") {
    $args += @("--raw-json", $RawJson)
}

if ($ChromePath -ne "") {
    $args += @("--chrome-path", $ChromePath)
}

if ($Headed) {
    $args += "--headed"
}

& $node @args
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
