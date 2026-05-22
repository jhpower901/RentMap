param(
    [string[]]$Urls = @(
        "https://new.land.naver.com/rooms?cortarNo=4111710200&a=APT:OPST:ABYG:OBYG:GM:OR:DDDGG:JWJT:SGJT:VL&e=RETAIL&aa=SMALLSPCRENT&warrantPrc=0:3000&rentPrc=0:60&order=rank",
        "https://new.land.naver.com/rooms?cortarNo=4111514000&a=APT:OPST:ABYG:OBYG:GM:OR:DDDGG:JWJT:SGJT:VL&e=RETAIL&aa=SMALLSPCRENT&warrantPrc=0:3000&rentPrc=0:60&order=rank",
        "https://new.land.naver.com/rooms?cortarNo=4111710100&a=APT:OPST:ABYG:OBYG:GM:OR:DDDGG:JWJT:SGJT:VL&e=RETAIL&aa=SMALLSPCRENT&warrantPrc=0:3000&rentPrc=0:60&order=rank"
    ),
    [string]$OutputCsv = ".\data\naver_land_ajou_2026-05-22.csv",
    [string]$RawJson = "",
    [int]$MaxPages = 5,
    [string]$ChromePath = "",
    [switch]$Headed,
    [double]$MinLat = 37.265,
    [double]$MaxLat = 37.285,
    [double]$MinLng = 127.030,
    [double]$MaxLng = 127.055
)

$ErrorActionPreference = "Stop"

$runtimeRoot = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\node"
$node = Join-Path $runtimeRoot "bin\node.exe"
$nodeModules = Join-Path $runtimeRoot "node_modules"
$playwrightCoreModules = Join-Path $nodeModules ".pnpm\playwright@1.60.0\node_modules"

if (-not (Test-Path $node)) {
    throw "Bundled Node.js was not found at $node"
}

$env:NODE_PATH = "$nodeModules;$playwrightCoreModules"

$scriptPath = Join-Path $PSScriptRoot "crawl_naver_land.js"

$urlArgs = @()
foreach ($u in $Urls) {
    $urlArgs += @("--url", $u)
}

$nodeArgs = $urlArgs + @(
    "--output-csv", $OutputCsv,
    "--max-pages", "$MaxPages",
    "--min-lat", "$MinLat",
    "--max-lat", "$MaxLat",
    "--min-lng", "$MinLng",
    "--max-lng", "$MaxLng",
    "--skip-home"
)

if ($RawJson -ne "") {
    $nodeArgs += @("--raw-json", $RawJson)
}

if ($ChromePath -ne "") {
    $nodeArgs += @("--chrome-path", $ChromePath)
}

if ($Headed) {
    $nodeArgs += "--headed"
}

& $node $scriptPath @nodeArgs
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
