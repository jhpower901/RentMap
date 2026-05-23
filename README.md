# RentMap — 크롤링 명령 치트시트

다방 / 직방 / 당근 / 네이버부동산을 크롤해서 지도 + 표 형태의 정적 페이지로 만드는 도구.

기본 동작은 두 컨테이너(`rentmap-server`, `rentmap-naver`)의 **APScheduler**가 매시간 `:00`에 자동 실행하기 때문에 평소엔 손 댈 일이 없습니다. 이 문서는 **수동으로 한 번 더 돌리고 싶을 때** 어떤 명령을 어떻게 쓰는지만 정리한 치트시트.

플랫폼별 상세 동작은 [`docs/`](docs/) 아래의 개별 문서 참고.

---

## 0. 컨테이너 구성

| 컨테이너 | 역할 | 자동 스케줄 |
|---|---|---|
| `rentmap-server` | dabang / zigbang / daangn 크롤 + `gen-web` + 웹서버(:8000) | 크롤 매시간 `:00`, `gen-web` `:00` / `:30` |
| `rentmap-naver` | naver 크롤 (playwright) | 매시간 `:00` |

두 컨테이너는 `./data`, `./scripts` 볼륨을 공유하므로 어느 쪽이 CSV를 쓰든 상대편의 `gen-web`이 자동으로 집어듭니다.

```sh
docker compose up -d           # 두 컨테이너 모두 기동
docker compose ps              # 상태 확인
docker compose logs -f rentmap # 스케줄러 로그 (크롤/gen-web 진행)
```

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
| `data/<source>_ajou_<YYYY-MM-DD>.csv` | 크롤 원본 |
| `data/naver_land_ajou_<DATE>.raw.json` | 네이버 원본 응답 (디버깅용) |
| `web/data_<source>.js` | `gen-web`이 만든 페이지 데이터 |
| `web/<source>.html` | 플랫폼별 페이지 |
| `web/favorites.html` | 좋아요 페이지 (브라우저 localStorage 기반) |
| `web/platform-common.{js,css}` | 4개 페이지 공통 모듈 |

---

## 7. 더 깊은 문서

- [`docs/dabang-crawling.md`](docs/dabang-crawling.md)
- [`docs/zigbang-crawling.md`](docs/zigbang-crawling.md)
- [`docs/daangn-crawling.md`](docs/daangn-crawling.md)
- [`docs/naver-land-crawling.md`](docs/naver-land-crawling.md)
- [`docs/data-schema-analysis.md`](docs/data-schema-analysis.md)
- [`docs/data-normalization-audit.md`](docs/data-normalization-audit.md)
- [`docs/web-runtime-notes.md`](docs/web-runtime-notes.md)
- [`docs/docker.md`](docs/docker.md)
- [`docs/configurable-crawl-roadmap.md`](docs/configurable-crawl-roadmap.md)
