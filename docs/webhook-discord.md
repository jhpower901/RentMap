# Discord Webhook 운영 가이드

RentMap의 reconcile 엔진이 감지한 매물 변동 — 신규/가격변경/상세변경/누락/삭제/재등장 — 을 Discord 채널로 자동 알림합니다.

## 1. 흐름 요약

```
크롤 종료
  ↓
reconcile_crawl  →  listing_status_events INSERT (webhook_sent_at = NULL)
                                 ↓
                    crawl/reconcile 완료 직후 webhook_worker.flush_once
                                 ↓
                         Discord embed POST
                                 ↓
                    성공: webhook_sent_at = now()
                    429:   Retry-After 존중
                    실패:  exponential backoff (2/4/8/16/32분, max 5회)
```

이벤트 자체는 영구 보존 (분석/감사용). `webhook_sent_at` 만 발송 여부를 표시.

## 2. Discord webhook URL 발급

1. Discord에서 알림 받을 **서버** 선택 → `서버 설정` → `연동(Integrations)` → `웹후크` → `새 웹후크`
2. 이름 / 채널 / 아바타 설정 후 `URL 복사`
3. URL 형태: `https://discord.com/api/webhooks/<webhook_id>/<token>`

> **이 URL은 시크릿입니다.** URL을 가진 누구나 그 채널에 글을 쓸 수 있어요. 공개 저장소·이미지·로그에 절대 박지 마세요.

## 3. RentMap에 설정

RentMap 폴더의 `.env`에 추가:

```env
RENTMAP_DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/.../...
```

값이 비어있으면 worker는 silent no-op — 이벤트는 큐에 쌓이지만 발송하지 않습니다 (나중에 URL 설정하면 다음 crawl/reconcile 완료 시 drain).

설정 후 RentMap 컨테이너 재시작:

```sh
docker compose restart rentmap
```

crawl/reconcile이 완료될 때 워커가 호출되므로 다음 성공한 크롤 완료 직후 자동 발송 시작.

## 4. 첫 활성화 전 — Dry-run으로 관찰 (권장)

빈 DB에 백필을 처음 부으면 수천 건의 `discovered` 이벤트가 생깁니다. 그것이 전부 Discord로 가면 채널이 한 번에 폭주.

방어 두 단계:

### 4.1 백필 시 (`scripts/backfill.py` 기본 동작)

```sh
docker exec rentmap-server python scripts/backfill.py
```

기본이 `--dry-run-webhooks` — 이벤트가 INSERT되면서 `webhook_sent_at`도 즉시 채워져 worker가 발송하지 않음. `--live` 플래그를 의도적으로 줘야만 발송이 일어남.

### 4.2 reconcile 첫 cron 시 (`RENTMAP_RECONCILE_DRY_RUN_WEBHOOKS`)

매시간 cron이 reconcile을 호출할 때도 같은 방어를 적용하려면 `.env`에 추가:

```env
RENTMAP_RECONCILE_DRY_RUN_WEBHOOKS=1
```

1~2 cron 사이클 (1~2시간) 동안 어떤 이벤트가 어느 정도 비율로 생기는지 관찰. 합리적 양이면 `0`으로 바꾸거나 줄을 지우고 컨테이너 재시작.

큐가 어떻게 차고 있는지 확인:

```sh
docker exec rentmap-server bash -c "cd /app && python scripts/webhook_worker.py pending"
# → queue: {'deliverable': 152, 'giving_up': 0, 'sent_total': 6140}
```

`deliverable`이 발송 대기. 활성화하면 다음 crawl/reconcile 완료 직후 한 번에 drain.

## 5. 이벤트 타입과 embed 색상

| event_type | 색상 | 의미 |
|---|---|---|
| `discovered` | 🟢 초록 | 새 매물 등장 |
| `price_changed` | 🟡 노랑 | 가격 (보증금/월세/관리비) 중 하나 이상 변동 |
| `detail_changed` | 🔵 파랑 | 가격 외 상세 변동. 현재 Discord 알림은 보내지 않고 처리 완료 |
| `missing` | 🟠 주황 | 같은 스케줄 안에서 1~2회 누락. 재시도 대기 상태이며 Discord 알림 없음 |
| `removed` | 🔴 빨강 | 스케줄 내부 2회 재시도 후에도 누락되어 삭제 확정 |
| `reappeared` | 🔵 파랑 | 누락됐던 매물 재등장 |

각 embed에는 매물 제목 + 위치 + 가격 + 매물 정보 + 썸네일 이미지가 포함됩니다. 가격 변경은 이전/현재 가격을 함께 표시하고, 삭제 알림은 마지막으로 저장된 원래 매물 정보를 표시합니다.

## 6. Rate limit / 백오프

- Worker 자체의 분당 cap은 없음. 한 번의 flush에서 현재 deliverable 큐를 모두 drain.
- Discord가 429를 반환하면 서버가 준 `Retry-After`를 존중하고 해당 row만 큐에 남김.
- 429 응답: `Retry-After` 헤더 존중 → 그 시각 이후 재시도.
- 다른 실패 (네트워크/5xx): 지수 백오프 — 2/4/8/16/32분.
- 5회 시도 후에도 실패: row가 `webhook_attempts >= 5`로 멈춤. `webhook_last_error`에 마지막 에러. 사람이 확인 후 reset 필요:

```sh
docker exec rentmap-postgres psql -U rentmap -d rentmap -c "
UPDATE listing_status_events
SET webhook_attempts = 0, webhook_next_try_at = NULL, webhook_last_error = NULL
WHERE webhook_sent_at IS NULL AND webhook_attempts >= 5;"
```

## 7. 시끄러우면 (필터링)

현재 정책은 신규/가격변경/삭제/재등장 중심입니다. 순수 상세 변경과 일시 누락은 Discord 알림을 보내지 않습니다. 첫 며칠 관찰 후 더 줄이고 싶으면:

### 7.1 이벤트 타입 필터링

`scripts/webhook_worker.py`의 `_fetch_batch` SQL에서 `WHERE` 절을 좁힘:

```sql
WHERE e.event_type IN ('price_changed', 'removed', 'reappeared')
```

`missing` / `detail_changed`는 기본적으로 이미 조용히 처리됩니다.

### 7.2 좋아요 한정

좋아요한 매물만 알림 받으려면 `listings`에 `favorites_persistent.json`을 join. 별도 feature 필요 (현재 미구현).

### 7.3 임시 정지

```sh
# .env에서 RENTMAP_DISCORD_WEBHOOK_URL 한 줄 주석 처리
docker compose restart rentmap
```

worker는 URL 없으면 silent no-op. 큐는 그대로 차오름 — URL 다시 설정하면 다음 crawl/reconcile 완료 시 drain.

## 8. 큐가 너무 차오를 때

이미 보낼 가치가 없는 옛 이벤트를 한 번에 보낸 걸로 처리:

```sh
docker exec rentmap-postgres psql -U rentmap -d rentmap -c "
UPDATE listing_status_events
SET webhook_sent_at = now()
WHERE webhook_sent_at IS NULL AND event_at < now() - interval '1 day';"
```

또는 모든 큐 한 번에 비우기 (이벤트 자체는 보존):

```sh
docker exec rentmap-postgres psql -U rentmap -d rentmap -c "
UPDATE listing_status_events
SET webhook_sent_at = now()
WHERE webhook_sent_at IS NULL;"
```

## 9. 디버깅

```sh
# 최근 이벤트 5개
docker exec rentmap-postgres psql -U rentmap -d rentmap -c "
SELECT id, event_type, event_at, webhook_sent_at, webhook_attempts, webhook_last_error
FROM listing_status_events ORDER BY id DESC LIMIT 5;"

# 발송 실패 누적
docker exec rentmap-postgres psql -U rentmap -d rentmap -c "
SELECT event_type, COUNT(*) AS stuck
FROM listing_status_events
WHERE webhook_sent_at IS NULL AND webhook_attempts >= 1
GROUP BY event_type;"

# Worker scheduler 로그
docker compose logs -f rentmap | grep -E "webhook-flush|reconcile"
```

## 10. 보안

- Webhook URL 노출 시: Discord에서 해당 webhook 삭제 → 새로 만든 URL로 `.env` 업데이트 → 컨테이너 재시작.
- `.env`는 `.gitignore`에 포함됨. `git status`로 절대 추적되지 않게 확인.
- 백업 시 (`db-stack/backups/`) DB dump에는 webhook URL이 없음 (URL은 .env에만 존재). dump는 자유롭게 공유 가능.
