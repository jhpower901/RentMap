# RentMap — 크롤링 명령 치트시트

다방 / 직방 / 당근 / 네이버부동산을 크롤해서 지도 + 표 형태의 정적 페이지로 만드는 도구.

기본 동작은 두 컨테이너(`rentmap-server`, `rentmap-naver`)의 **APScheduler**가 매시간 `:00`에 자동 실행하기 때문에 평소엔 손 댈 일이 없습니다. 이 문서는 **수동으로 한 번 더 돌리고 싶을 때** 어떤 명령을 어떻게 쓰는지만 정리한 치트시트.

플랫폼별 상세 동작은 [`docs/`](docs/) 아래의 개별 문서 참고.

---

## 0. 컨테이너 구성

| 컨테이너 | 위치 | 역할 | 자동 스케줄 |
|---|---|---|---|
| `rentmap-server` | RentMap | dabang / zigbang / daangn 크롤 + `gen-web` + 웹서버(:8000) + webhook worker | 크롤 매시간 `:00`, `gen-web` 매시간 `:50`, webhook은 크롤 완료 직후 |
| `rentmap-naver`  | RentMap | naver 크롤 (playwright) | 매시간 `:00` |
| `rentmap-postgres` | [`../db-stack/`](../db-stack/) (별 compose) | 매물 history DB. 크롤 후 reconcile이 incremental snapshot 적재 | — (상시) |

세 컨테이너는 `rentmap-db` 외부 네트워크로 연결됩니다. RentMap의 `docker-compose.yml`은 그 네트워크를 `external: true`로 참조하므로 **db-stack을 먼저 띄워야** RentMap이 정상 기동.

```sh
# 1) DB 먼저
cd ../db-stack && docker compose up -d

# 2) RentMap (DB 네트워크에 붙으면서 기동)
cd ../RentMap && docker compose up -d

# 상태 확인
docker compose ps                              # RentMap 두 컨테이너
docker ps --filter name=rentmap-postgres       # DB
docker compose logs -f rentmap                 # 크롤/gen-web/webhook 진행
```

**환경변수**: RentMap 폴더의 `.env`에서 `RENTMAP_DB_URL`, `RENTMAP_DISCORD_WEBHOOK_URL` 등 설정. 템플릿은 [`.env.example`](.env.example) 참고.

---

## 1. 수동 크롤 — 빠른 방법 (이미 도는 컨테이너에서)

가장 흔히 쓰는 패턴. 이미 `rentmap-server` / `rentmap-naver`가 떠 있을 때, 그 안에서 즉시 한 번 더 돌립니다.

```sh
# 다방 + 직방 + 당근 한 방에 (병렬, gen-web까지)
docker exec rentmap-server python scripts/rentmap.py crawl-all --skip-naver --gen-web

# 네이버만 추가로
docker exec rentmap-naver python scripts/rentmap.py crawl-naver

# 4개 다 (네이버는 별 컨테이너에서 직접 도는 게 정석이지만, 굳이 한 컨테이너에서 다 돌리고 싶다면)
docker exec rentmap-server python scripts/rentmap.py crawl-all --gen-web
```

> 💡 `--gen-web` 플래그를 붙이면 크롤이 끝나는 즉시 `data_*.js` / HTML이 재생성됩니다. 안 붙이면 다음 정시 스케줄러가 처리할 때까지 웹에 안 반영돼요.

---

## 2. 수동 크롤 — Ad-hoc 컨테이너 (`scripts/docker.sh`)

`docker compose run --rm`으로 일회용 컨테이너를 띄워서 돌립니다. 격리가 필요하거나, 메인 컨테이너가 멈춰있을 때 유용.

```sh
# 헬퍼 스크립트 (Linux/macOS/WSL)
./scripts/docker.sh crawl-all --skip-naver --gen-web
./scripts/docker.sh gen-web

# Windows PowerShell
./scripts/docker.ps1 crawl-all --skip-naver --gen-web

# 네이버용 ephemeral 컨테이너
./scripts/docker-naver.sh crawl-naver
./scripts/docker-naver.ps1 crawl-naver
```

내부적으론 그냥 `docker compose run --rm rentmap python scripts/rentmap.py "$@"` 한 줄 래퍼.

---

## 3. Subcommand 레퍼런스

모든 명령은 `python scripts/rentmap.py <subcommand> [options]` 형식.

### `crawl-all` — 3개(또는 4개) 병렬 크롤

```sh
python scripts/rentmap.py crawl-all [options]
```

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `--date` | 오늘 (KST) | CSV 파일명 날짜 suffix |
| `--center-lat` | env `RENTMAP_CENTER_LAT` | 검색 중심 위도 |
| `--center-lng` | env `RENTMAP_CENTER_LNG` | 검색 중심 경도 |
| `--radius-km` | env `RENTMAP_RADIUS_KM` | 검색 반경 |
| `--skip-naver` | off | naver 빼고 3개만 (보통 켜둠 — naver는 별도 컨테이너) |
| `--gen-web` | off | 끝나고 gen-web 자동 실행 |

`ThreadPoolExecutor`로 각 플랫폼이 독립 스레드에서 도므로 셋이 동시에 끝납니다. 한 곳이 실패해도 나머지는 계속 진행.

### `crawl-dabang`

```sh
python scripts/rentmap.py crawl-dabang [options]
```

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `--center-lat / --center-lng / --radius-km` | env 기반 | 중심+반경으로 bbox 자동 계산 |
| `--min-lat / --max-lat / --min-lng / --max-lng` | env 기반 | bbox 직접 지정 |
| `--zoom` | 16 | 다방 지도 줌 레벨 |
| `--max-deposit` / `--max-rent` | `999999` | 만원 단위 상한 |
| `--output-csv` | `data/dabang_ajou_<DATE>.csv` | 출력 경로 |
| `--raw-json` | 비활성 | 원본 응답을 추가로 덤프할 경로 |
| `--delay-ms` | 적당히 | 요청 간 슬립 |

### `crawl-zigbang`

```sh
python scripts/rentmap.py crawl-zigbang [options]
```

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `--center-lat/lng / --radius-km` *또는* `--min/max-lat/lng` | env 기반 | bbox |
| `--geohashes` | 아주대 주변 geohash 세트 | 직방 API용 geohash 리스트 |
| `--max-deposit-manwon` / `--max-rent-manwon` | `999999` | 상한 |
| `--output-csv` | `data/zigbang_ajou_<DATE>.csv` | 출력 |

### `crawl-daangn`

```sh
python scripts/rentmap.py crawl-daangn [options]
```

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `--region-ids` | env `RENTMAP_DAANGN_REGION_IDS` (없으면 수원 권선구 기본) | 당근 region ID 목록 |
| `--max-deposit` / `--max-rent` | `999999` | 상한 |
| `--output-csv` | `data/daangn_ajou_<DATE>.csv` | 출력 |
| `--skip-detail` | off | SSR HTML 상세 파싱(=description/시설옵션) 생략. 빠른 스모크 테스트용 |
| `--center-lat/lng / --radius-km` *또는* `--min/max-lat/lng` | env 기반 | bbox (post-fetch 필터) |

> 당근은 region ID 기반이라 bbox는 받아온 결과를 거르는 용도일 뿐. 새 지역 ID는 `daangn.com/kr/realty/?in=x-XXXX` URL에서 확인.

### `crawl-naver`

```sh
python scripts/rentmap.py crawl-naver [options]
```

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `--center-lat/lng / --radius-km` *또는* `--min/max-lat/lng` | env 기반 | bbox (없으면 자동 `ms=` 그리드 생성) |
| `--url` (반복 가능) | 비활성 | 특정 네이버부동산 URL만 강제 사용 |
| `--max-pages` | 20 | cortarNo 당 페이지 상한 (20 × 100 = ~2000건) |
| `--skip-home` | off | 홈페이지 워밍업 호출 생략 |
| `--skip-detail` | off | 상세 API enrichment(주소/방수/주차/description/사진 등) 생략. 빠른 스모크용 |
| `--headed` | off | playwright 헤드 모드 (디버깅용) |
| `--chrome-path` | 빈값 | 커스텀 Chrome 바이너리 경로 |
| `--output-csv` | `data/naver_land_ajou_<DATE>.csv` | 출력 |
| `--raw-json` | 비활성 | 원본 응답 덤프 경로 |

추가로 env `RENTMAP_NAVER_CORTARNOS`로 명시 cortarNo를 강제 페이지네이션 — `ms=` → cortarNo 매핑이 비결정적이라 backstop 필요. 자세한 건 [`docs/naver-land-crawling.md`](docs/naver-land-crawling.md).

### `gen-web` — CSV → HTML/JS

```sh
python scripts/rentmap.py gen-web [options]
```

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `--data-dir` | `./data` | CSV 입력 디렉터리 |
| `--out-dir` | `./web` | HTML/JS 출력 디렉터리 |
| `--date` | 오늘 | 어느 날짜 CSV를 쓸지. 없으면 가장 최근으로 fallback |

크롤이 없어도 기존 CSV로 페이지만 재생성할 때 유용.

---

## 4. 환경변수

`docker-compose.yml`에 기본값이 박혀 있고 `.env` 파일로 오버라이드 가능.

| 변수 | 기본값 | 설명 |
|---|---|---|
| `RENTMAP_AREA_NAME` | `아주대` | UI 헤더에 노출되는 지역명 |
| `RENTMAP_CENTER_LAT` | `37.280062` | 검색 중심 위도 |
| `RENTMAP_CENTER_LNG` | `127.043688` | 검색 중심 경도 |
| `RENTMAP_RADIUS_KM` | `3.0` | 검색 반경 |
| `RENTMAP_MAX_DEPOSIT` | `999999` | 보증금 상한(만원) |
| `RENTMAP_MAX_RENT` | `999999` | 월세 상한(만원) |
| `RENTMAP_DAANGN_REGION_IDS` | `""` (수원 권선구로 fallback) | 콤마 구분. 새 지역은 daangn.com에서 ID 확인 |
| `RENTMAP_NAVER_URLS` | `""` | 파이프(`\|`) 구분. 비우면 bbox로 그리드 자동 생성 |
| `RENTMAP_NAVER_CORTARNOS` | 아주대 인근 19개 동 코드 | 비결정적 `ms=` 매핑 backstop. 콤마 구분 |
| `TZ` | `Asia/Seoul` | 스케줄러 타임존 |

---

## 5. 자주 쓰는 패턴 모음

```sh
# 지금 당장 dabang/zigbang/daangn 새로 받아서 웹에 반영
docker exec rentmap-server python scripts/rentmap.py crawl-all --skip-naver --gen-web

# 네이버만 한 번 더
docker exec rentmap-naver python scripts/rentmap.py crawl-naver

# 크롤 없이 HTML만 재생성 (코드 수정 후 빠른 반영)
docker exec rentmap-server python scripts/rentmap.py gen-web

# 특정 날짜 CSV로 페이지 다시 만들기 (롤백/디버깅)
docker exec rentmap-server python scripts/rentmap.py gen-web --date 2026-05-23

# 다른 지역 한 번만 시험 (env 안 건드리고)
docker exec rentmap-server python scripts/rentmap.py crawl-dabang \
  --center-lat 37.5665 --center-lng 126.9780 --radius-km 2.0 \
  --output-csv /tmp/seoul_test.csv

# 네이버 빠른 스모크 (detail 생략, 페이지 적게)
docker exec rentmap-naver python scripts/rentmap.py crawl-naver \
  --skip-detail --max-pages 2

# 스케줄러 로그 실시간
docker compose logs -f rentmap rentmap-naver
```

---

## 6. 결과물 위치

| 파일 | 의미 |
|---|---|
| `data/<source>_ajou_<YYYY-MM-DD>.csv` | 크롤 원본 (canonical "이번 정시 결과") |
| `data/naver_land_ajou_<DATE>.raw.json` | 네이버 원본 응답 (디버깅용) |
| `web/data_<source>.js` | `gen-web`이 만든 페이지 데이터 |
| `web/<source>.html` | 플랫폼별 페이지 |
| `web/favorites.html` | 좋아요 페이지 (브라우저 localStorage 기반) |
| `web/listing-info.js` | 매물 detail 패널 + 가격 sparkline 렌더 (공유 모듈) |
| `web/platform-common.{js,css}` | 4개 페이지 공통 모듈 |
| Postgres `rentmap-postgres` | 매물 history (snapshots, price_snapshots, events). 별 compose stack에서 운영 |

---

## 7. DB 운영 명령

### 7.1 마이그레이션 (`scripts/migrate.py`)

```sh
# 적용된 / 보류 마이그레이션 확인
docker exec rentmap-server bash -c "cd /app && python scripts/migrate.py status"

# 보류 마이그레이션 모두 적용
docker exec rentmap-server bash -c "cd /app && python scripts/migrate.py up"

# 특정 버전까지만 적용
docker exec rentmap-server bash -c "cd /app && python scripts/migrate.py up --to 001"
```

> 파일명은 `db/migrations/NNN_name.sql` 규칙. 이미 적용된 파일의 sha256이 바뀌면 거부 — 수정은 새 마이그레이션으로 fix-forward.

### 7.2 백필 (`scripts/backfill.py`) — CSV → DB 시드

```sh
# data/ 디렉터리의 모든 *_ajou_*.csv를 시간순으로 적재 (dry-run-webhooks 기본)
docker exec rentmap-server python scripts/backfill.py

# 특정 날짜만
docker exec rentmap-server python scripts/backfill.py --date 2026-05-24

# 단일 파일
docker exec rentmap-server python scripts/backfill.py --csv data/dabang_ajou_2026-05-24.csv

# webhook 실 발송 허용 (위험 — 큰 backlog 시 Discord 폭주)
docker exec rentmap-server python scripts/backfill.py --live
```

기본은 **dry-run-webhooks** — 이벤트가 큐에 들어가도 `webhook_sent_at`가 즉시 마킹돼 worker가 발송하지 않음. 빈 DB에 6,000행을 부으면서 6,000개 Discord 알림이 날아가는 사고 방지용.

### 7.3 Webhook worker (`scripts/webhook_worker.py`)

평소엔 크롤/reconcile이 성공한 직후 scheduler가 자동 호출. 수동 실행도 가능:

```sh
# 큐 상태
docker exec rentmap-server bash -c "cd /app && python scripts/webhook_worker.py pending"

# 1회 flush (기본: 현재 대기열 전체)
docker exec rentmap-server bash -c "cd /app && python scripts/webhook_worker.py flush"

# HTTP 안 보내고 sent로만 마킹 (테스트용)
docker exec rentmap-server bash -c "cd /app && python scripts/webhook_worker.py flush --dry-run"
```

Discord 발급/설정/dry-run 운용은 [`docs/webhook-discord.md`](docs/webhook-discord.md) 참고.

### 7.4 gen-web 데이터 소스 (`--source`)

```sh
# 기본 (auto): DB 우선, 비어있으면 source별로 CSV fallback
docker exec rentmap-server python scripts/rentmap.py gen-web

# DB만 (백필/reconcile 결과 확인용)
docker exec rentmap-server python scripts/rentmap.py gen-web --source db

# CSV만 (DB 다운 시 안전망)
docker exec rentmap-server python scripts/rentmap.py gen-web --source csv
```

### 7.5 가격 추이 API

브라우저에서 매물 펼침 시 `listing-info.js`가 lazy 호출:

```
GET /api/listings/{source}/{listing_no}/price-history?limit=60
```

응답: `{"points": [{"t":"ISO datetime","deposit":N,"rent":N,"maint":N,"total":N}, ...]}` (시간 오름차순). 1점 이하면 sparkline 안 그림.

### 7.6 운영 체크리스트

```sh
# DB 상태
docker exec rentmap-postgres pg_isready -U rentmap

# 최근 크롤 결과
docker exec rentmap-postgres psql -U rentmap -d rentmap -c "
SELECT p.code, c.started_at, c.finished_at, c.total_saved
FROM crawl_runs c JOIN platforms p ON p.id=c.platform_id
ORDER BY c.id DESC LIMIT 8;"

# 미발송 이벤트
docker exec rentmap-server bash -c "cd /app && python scripts/webhook_worker.py pending"

# DB 용량
docker exec rentmap-postgres psql -U rentmap -d rentmap -c "
SELECT pg_size_pretty(pg_database_size('rentmap'));"
```

---

## 8. 더 깊은 문서

- [`docs/db-history-schema.md`](docs/db-history-schema.md) — DB 스키마 + 적재 흐름 + 정책 + 용량 예산
- [`docs/webhook-discord.md`](docs/webhook-discord.md) — Discord webhook 발급/설정/dry-run/안전 운용
- [`docs/dabang-crawling.md`](docs/dabang-crawling.md)
- [`docs/zigbang-crawling.md`](docs/zigbang-crawling.md)
- [`docs/daangn-crawling.md`](docs/daangn-crawling.md)
- [`docs/naver-land-crawling.md`](docs/naver-land-crawling.md)
- [`docs/data-schema-analysis.md`](docs/data-schema-analysis.md)
- [`docs/data-normalization-audit.md`](docs/data-normalization-audit.md)
- [`docs/web-runtime-notes.md`](docs/web-runtime-notes.md)
- [`docs/docker.md`](docs/docker.md)
- [`docs/configurable-crawl-roadmap.md`](docs/configurable-crawl-roadmap.md)
