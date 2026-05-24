# Docker usage

RentMap runs as two long-lived containers via `docker compose`:

- **`rentmap`** (lightweight, [Dockerfile](../Dockerfile)) — FastAPI server on
  port 8000 (favorites/photos persistence) plus an APScheduler with two cron
  jobs (all times KST):
  - **Every hour at :00** — `crawl-all --skip-naver` (dabang + zigbang + daangn)
  - **Every hour at :50** — `gen-web` regenerates platform HTML
    pages from whatever CSVs are currently on disk
  - **After crawl/reconcile completes** — webhook events are flushed to Discord
    from the DB queue
- **`rentmap-naver`** ([Dockerfile.naver](../Dockerfile.naver), Playwright base)
  — runs `crawl-naver` every hour at :00 in lock-step with the main container.
  Shares the `./data` volume so the next gen-web tick picks up fresh CSVs, and
  flushes webhook events after its own crawl/reconcile completes.

`gen-web` is fault-tolerant: if today's CSV for some source is missing (first
boot, slow naver crawl still running, etc.) it falls back to the most recent
CSV for that source. The web stays usable even when one source is mid-crawl
or has failed.

The main container also runs startup-kick crawl and `gen-web` jobs shortly
after boot so a fresh stack has pages without waiting for the first `:50` tick.

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

- Hourly crawl (dabang/zigbang/daangn): `CronTrigger(minute=0, ...)` in
  [scripts/server.py](../scripts/server.py), job id `hourly_crawl`.
- gen-web cadence: `CronTrigger(minute=50, ...)` in the same file, job id
  `gen_web_hourly_50`.
- Naver crawl: `CronTrigger(minute=0, ...)` in
  [scripts/scheduler_naver.py](../scripts/scheduler_naver.py).

Restart the affected container(s) after changing (`docker compose restart rentmap`
or `... rentmap-naver`).
