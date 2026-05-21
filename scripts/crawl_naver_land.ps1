param(
    [string]$Url = "https://new.land.naver.com/rooms?ms=2AzVQ9,3zkrDJ,17&a=APT:OPST:ABYG:OBYG:GM:OR:DDDGG:JWJT:SGJT:VL&e=RETAIL&aa=SMALLSPCRENT",
    [string]$OutputCsv = ".\data\naver_land_ajou_2026-05-22.csv",
    [string]$RawJson = "",
    [int]$MaxPages = 5,
    [string]$ChromePath = "",
    [switch]$Headed
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
    "--max-pages", "$MaxPages"
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
