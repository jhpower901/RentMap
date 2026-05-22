# Docker usage

RentMap can run the Python crawlers and web-data generator in Docker on Windows
and Linux. The default image is intentionally lightweight and supports Dabang,
Zigbang, Daangn, and web generation. Naver uses a separate Playwright image
because it needs a browser.

## Build

```powershell
docker compose build
```

The same command works in Linux shells.

For routine work, build only the lightweight service:

```powershell
docker compose build rentmap
```

Build the Naver service only when you need Naver crawling. It uses a Playwright
browser image and is much larger than the default crawler image.

## Run commands

```powershell
docker compose run --rm rentmap --help
docker compose run --rm rentmap crawl-dabang
docker compose run --rm rentmap crawl-zigbang
docker compose run --rm rentmap crawl-daangn
docker compose run --rm rentmap gen-web
docker compose run --rm rentmap crawl-all --skip-naver --gen-web
```

Naver crawler:

```powershell
docker compose --profile naver build rentmap-naver
docker compose --profile naver run --rm rentmap-naver crawl-naver
```

Windows PowerShell wrapper:

```powershell
.\scripts\docker.ps1 gen-web
.\scripts\docker.ps1 crawl-all --skip-naver --gen-web
.\scripts\docker-naver.ps1 crawl-naver
```

Linux/macOS shell wrapper:

```sh
sh scripts/docker.sh gen-web
sh scripts/docker.sh crawl-all --skip-naver --gen-web
sh scripts/docker-naver.sh crawl-naver
```

The repository is mounted into `/app`, so generated CSV and web files are written
back to the working tree.
