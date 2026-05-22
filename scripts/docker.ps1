param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $RentMapArgs
)

if (-not $RentMapArgs -or $RentMapArgs.Count -eq 0) {
    $RentMapArgs = @("--help")
}

docker compose run --rm rentmap @RentMapArgs
