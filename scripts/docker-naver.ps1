param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $RentMapArgs
)

if (-not $RentMapArgs -or $RentMapArgs.Count -eq 0) {
    $RentMapArgs = @("crawl-naver")
}

# Ad-hoc naver-side rentmap subcommand. rentmap-naver already runs the
# every-3h scheduler; this is for manual one-off runs.
docker compose run --rm rentmap-naver python scripts/rentmap.py @RentMapArgs
