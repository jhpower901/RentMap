param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $RentMapArgs
)

if (-not $RentMapArgs -or $RentMapArgs.Count -eq 0) {
    $RentMapArgs = @("crawl-naver")
}

docker compose --profile naver run --rm rentmap-naver @RentMapArgs
