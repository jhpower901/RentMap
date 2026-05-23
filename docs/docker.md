# Docker usage

RentMap runs as two long-lived containers via `docker compose`:

- **`rentmap`** (lightweight, [Dockerfile](../Dockerfile)) — FastAPI server on
  port 8000 (favorites/photos persistence) plus an APScheduler that runs
  `crawl-all --skip-naver --gen-web` every hour at :05 KST.
- **`rentmap-naver`** ([Dockerfile.naver](../Dockerfile.naver), Playwright base)
  — runs `crawl-naver` every 3 hours at :30 KST. Shares the `./data` volume so
  the main container's next hourly gen-web includes fresh naver data.

Both containers run a startup-kick crawl shortly after boot, so a fresh stack
populates `data/` and `web/` without waiting for the first cron tick.

## Build & start

```powershell
docker compose build
docker compose up -d
docker compose logs -f rentmap rentmap-naver
```

The repository is bind-mounted into `/app`, so generated CSVs (`data/*.csv`)
and generated web pages (`web/*.html`, `web/data_*.js`) appear on the host
filesystem.

## Manual one-off runs

The scheduler covers normal operation. Use the helpers below only for ad-hoc
work (run a specific subcommand immediately, regenerate web from existing
CSVs, debug a single source, etc.).

```powershell
# Light image (dabang / zigbang / daangn / gen-web)
docker compose run --rm rentmap python scripts/rentmap.py crawl-dabang
docker compose run --rm rentmap python scripts/rentmap.py gen-web
docker compose run --rm rentmap python scripts/rentmap.py crawl-all --skip-naver --gen-web

# Playwright image (naver)
docker compose run --rm rentmap-naver python scripts/rentmap.py crawl-naver
```

Convenience wrappers (same effect):

```powershell
# PowerShell
.\scripts\docker.ps1 gen-web
.\scripts\docker.ps1 crawl-all --skip-naver --gen-web
.\scripts\docker-naver.ps1 crawl-naver
```

```sh
# bash / sh
sh scripts/docker.sh gen-web
sh scripts/docker-naver.sh crawl-naver
```

## Tuning the schedule

- Hourly run: edit `CronTrigger(minute=5, ...)` in
  [scripts/server.py](../scripts/server.py).
- Naver run: edit `CronTrigger(hour='*/3', minute=30, ...)` in
  [scripts/scheduler_naver.py](../scripts/scheduler_naver.py).

Restart the container(s) after changing.
