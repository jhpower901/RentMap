param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $RentMapArgs
)

if (-not $RentMapArgs -or $RentMapArgs.Count -eq 0) {
    $RentMapArgs = @("--help")
}

# Ad-hoc rentmap subcommand against the lightweight image. The long-running
# rentmap-server already runs the hourly scheduler; this is for manual runs.
docker compose run --rm rentmap python scripts/rentmap.py @RentMapArgs
