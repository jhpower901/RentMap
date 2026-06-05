# RentMap DB-tool 운영 가이드

`scripts/dbtool/`에 들어있는 관리툴 운영/배포/접속 절차. 본 사이트(`scripts/server.py`)와 별 프로세스로 돌아서 관리툴이 죽거나 들끓어도 본 사이트 SLA에 영향이 없도록 설계.

---

## 1. 무엇이 들어있나

| 영역 | 위치 | 비고 |
|---|---|---|
| FastAPI 앱 | `scripts/dbtool/server.py` | 별 ASGI 앱, 포트 8001 |
| 라우터 | `scripts/dbtool/routes_*.py` | users / favorites / bookmarks / listings / events / regions / audit |
| 감사 로그 헬퍼 | `scripts/dbtool/audit.py` | `admin_audit_log` writer + reverse-SQL builders |
| 인증 | `scripts/dbtool/deps.py` | RentMap `users.is_admin=TRUE`만, 쿠키명 `rentmap_dbtool_session` |
| 정적 페이지 | `web/dbtool/` | login.html, index.html(SPA), app.css, app.js |
| 스키마 | `db/migrations/014_admin_audit_log.sql` | actor / target / before/after / reverse_sql |
| 컨테이너 | `docker-compose.yml` 의 `rentmap-dbtool` | 같은 이미지 재사용 (`image: rentmap-rentmap`) |

탭 구성:

1. **사용자** — 생성 / 표시이름·admin·active 변경 / 비번 재설정 / 세션 종료 / 삭제(사용자명 재입력 확인)
2. **좋아요** — 좋아요/싫어요 조회, 사용자 간 이전(`copy`/`move`, `skip`/`overwrite`), 일괄 삭제(tombstone 자동)
3. **북마크** — 실사 기록(`bookmarks`) 조회/이전/삭제. `sort_order` 유지
4. **매물** — 플랫폼/지역/상태 필터, 상세(지역별 상태 + 스냅샷 + 이벤트), `current_status` 단일/일괄 변경
5. **이벤트 큐** — `webhook_deliveries` 재시도/발송완료마킹, 미팬아웃 이벤트 모니터
6. **지역/스케줄** — `regions` + `region_schedules` CRUD (cron 검증)
7. **감사 로그** — 모든 변경 + 단일행 변경에 대한 **롤백 버튼**

---

## 2. 안전장치 요약

* 모든 mutate는 **단일 트랜잭션** 안에서 데이터 변경 + `admin_audit_log` 한 줄 동시 commit
* destructive 액션은 항상 **dry-run preview 모달** 먼저 → 실행 확인 모달 2단계
* 사용자/지역 삭제는 **아이디/슬러그 재입력** 확인
* 자기 자신 demote / deactivate / delete는 서버단 거부
* 비밀번호는 audit `cmd_payload`에 `***` 마스킹 저장 (원문 절대 안 들어감)
* 단일행 UPDATE/INSERT/DELETE에 한해 `reverse_sql` 보관 → 감사 탭에서 1-click 롤백
* 외부 노출 두 경로(127.0.0.1 + Caddy proxy network) 외에는 닫혀있음

---

## 3. 접속 방법

### 3.1 Caddy 뒤로 — 운영 권장

`rentmap-db.anzam.kr` 같은 별 도메인을 Caddy에 추가하고 reverse-proxy. dbtool 자체 admin 로그인 위에 Caddy의 `basic_auth`를 한 겹 더 두는 게 권장.

**Caddyfile** (`Caddyfile`에 추가):

```caddy
rentmap-db.anzam.kr {
    encode zstd gzip

    header {
        X-Content-Type-Options nosniff
        Referrer-Policy strict-origin-when-cross-origin
        Permissions-Policy "camera=(), microphone=(), geolocation=()"
        # 관리툴은 인덱싱 차단
        X-Robots-Tag "noindex, nofollow"
    }

    # 1차 가드: Caddy basic_auth. 2차는 dbtool 자체의 admin 로그인.
    # bcrypt 해시는 `caddy hash-password` 로 생성.
    #   $ docker exec <caddy-container> caddy hash-password --plaintext '내비번'
    basic_auth {
        admin <bcrypt-hash-여기에>
    }

    reverse_proxy rentmap-dbtool:8001 {
        header_up Host {host}
        header_up X-Real-IP {remote_host}
        header_up X-Forwarded-Proto {scheme}
    }
}
```

옵션: basic_auth 대신/추가로 **IP allowlist** 사용 가능:

```caddy
@trusted {
    remote_ip 1.2.3.0/24 10.0.0.0/8
}
handle @trusted {
    reverse_proxy rentmap-dbtool:8001 { ... }
}
handle {
    respond "Forbidden" 403
}
```

DNS는 `rentmap-db.anzam.kr` → 서버 공인 IP 로 A 레코드 추가. Caddy가 첫 요청 때 ACME로 인증서 자동 발급.

Caddy 컨테이너가 `proxy` 외부 네트워크에 붙어있어야 `rentmap-dbtool` 호스트네임이 풀립니다. `docker-compose.yml`의 `rentmap-dbtool` 서비스가 이미 `proxy` + `rentmap-db` 두 네트워크에 붙어있게 정의돼 있어서, `git pull` 후 `docker compose up -d` 만 하면 자동으로 양쪽 모두에 attach 됩니다.

### 3.2 SSH 터널 — Caddy 다운 시 / 운영자 선호

`127.0.0.1:8001:8001` 호스트 바인딩이 `docker-compose.override.yml`(로컬 dev 전용, `.gitignore`)에 정의돼 있으면 SSH 터널로도 들어갈 수 있습니다. 운영 서버에 똑같이 override를 두려면 다음 한 블록만 추가:

```yaml
# /opt/docker/RentMap/docker-compose.override.yml
services:
  rentmap-dbtool:
    ports:
      - "127.0.0.1:8001:8001"
```

이 후:

```sh
# 로컬 PC에서
ssh -L 8001:127.0.0.1:8001 <user>@rentmap.anzam.kr

# 브라우저
open http://127.0.0.1:8001/login.html
```

### 3.3 로컬 개발

`docker compose up -d rentmap-dbtool` 후 그냥 [http://127.0.0.1:8001](http://127.0.0.1:8001) 로 접속.

---

## 4. 첫 배포 — 운영 서버 (rentmap.anzam.kr)

### 4.1 코드 동기화

운영 서버 RentMap 폴더에서:

```sh
cd /opt/docker/RentMap
git fetch origin
git pull --ff-only
# 또는 deploy.sh가 알아서 backup→pull→build→up까지 해줍니다 (마이그레이션은 빼고):
bash scripts/deploy.sh
```

새로 들어오는 파일 (git pull 결과):

```
db/migrations/017_region_schedules_peterpan.sql
db/migrations/018_user_webhooks_region_ids_comment_and.sql
scripts/dbtool/                  (디렉터리 전체)
web/dbtool/                      (디렉터리 전체)
docker-compose.yml               (rentmap-dbtool 서비스 추가됨)
docs/dbtool.md                   (이 문서)
```

`docker-compose.override.yml`은 `.gitignore` 라 운영 서버 본인 것이 그대로 유지됩니다. ports 매핑이 필요 없으면(=Caddy로만 접근) 손댈 것 없음.

### 4.2 마이그레이션 적용 — deploy.sh와 별

`scripts/deploy.sh`는 의도적으로 마이그레이션을 자동 적용하지 않습니다 (스키마 변경은 운영자가 한 번 더 확인하는 게 안전). 별 단계로 다음을 실행:

```sh
# 1) 현재 상태 확인
docker exec rentmap-server bash -c "cd /app && python scripts/migrate.py status"
```

출력에서 두 가지를 확인:

* **`DRIFT!! 013_user_webhook_regions.sql`** 가 보이는지
* **`PENDING`** 라인들이 `015 / 016 / 017 / 018` 인지

#### 4.2.1 013 drift 해소 (한 번만)

`706b8e1 대현 건의 업데이트` 커밋에서 013의 코멘트가 OR → AND로 수정됐는데 sha가 변경되어 `migrate.py`가 거부합니다. 014~016이 이미 적용된 상태라면 이 단계가 필요:

```sh
docker exec rentmap-postgres psql -U rentmap -d rentmap -c "
UPDATE schema_migrations
   SET sha256 = 'a9bd934b2db0cb5114ec521b59c1be5ae78238814e7aab7a00d6553fa7db454f'
 WHERE filename = '013_user_webhook_regions.sql'
   AND sha256 <> 'a9bd934b2db0cb5114ec521b59c1be5ae78238814e7aab7a00d6553fa7db454f';"
# 정상 출력: UPDATE 1 (또는 이미 해소돼 있으면 UPDATE 0 — 안전하게 idempotent)
```

#### 4.2.2 마이그레이션 적용

```sh
docker exec rentmap-server bash -c "cd /app && python scripts/migrate.py up"
```

적용 순서:

* `015_bookmarks.sql` — bookmarks + bookmark_deleted 테이블
* `016_peterpan_platform.sql` — `platforms` 에 peterpan 행 추가
* `017_region_schedules_peterpan.sql` — `region_schedules.source` CHECK에 peterpan 포함
* `018_user_webhooks_region_ids_comment_and.sql` — `user_webhooks.region_ids` COMMENT를 AND 시맨틱으로 다시 새김

`migrate.py status` 다시 돌려서 모두 `applied` 인지 확인.

### 4.3 dbtool 빌드 + 띄움

`deploy.sh`가 `docker compose up -d` 까지 해주므로 보통은 자동으로 같이 올라옵니다. 처음 도입 시에는 명시적으로 빌드 한 번:

```sh
docker compose build rentmap-dbtool
docker compose up -d rentmap-dbtool
docker logs rentmap-dbtool --tail 20
```

기대 출력: `Uvicorn running on http://0.0.0.0:8001 (Press CTRL+C to quit)`

### 4.4 Caddy 갱신

`Caddyfile`에 §3.1의 `rentmap-db.anzam.kr` 블록 추가 후:

```sh
# Caddy 컨테이너 위치에서
docker exec <caddy-container> caddy reload --config /etc/caddy/Caddyfile
# 또는
docker restart <caddy-container>
```

ACME가 첫 요청 시 인증서를 발급할 동안 몇 초 대기 가능.

### 4.5 적용 검증

```sh
# (a) 새 테이블 + 새 플랫폼 + 새 CHECK + 새 COMMENT 확인
docker exec rentmap-postgres psql -U rentmap -d rentmap <<'EOF'
SELECT id, code FROM platforms ORDER BY id;
\dt bookmark*
SELECT pg_get_constraintdef(oid)
  FROM pg_constraint WHERE conname = 'region_schedules_source_check';
SELECT col_description('user_webhooks'::regclass, attnum)
  FROM pg_attribute
 WHERE attrelid = 'user_webhooks'::regclass AND attname = 'region_ids';
EOF

# (b) dbtool 헬스 (Caddy 통해)
curl -sI https://rentmap-db.anzam.kr/login.html | head -5

# (c) dbtool 헬스 (내부 직접)
docker exec rentmap-server curl -sI http://rentmap-dbtool:8001/login.html | head -5
```

---

## 5. 일상 운영

### 5.1 코드 변경만 (마이그레이션 없을 때)

```sh
bash scripts/deploy.sh
# scripts/dbtool/* 만 바뀌었다면:
bash scripts/deploy.sh --no-build   # 컨테이너 재시작만, 이미지 재빌드 X
```

`scripts/`는 volume mount이므로 `--no-build` 로도 변경 사항이 컨테이너에 들어갑니다. 단 dbtool은 uvicorn이 자동 reload 하지 않으므로:

```sh
docker restart rentmap-dbtool
```

### 5.2 마이그레이션 있는 변경

```sh
bash scripts/deploy.sh                              # 코드 + 이미지 빌드
docker exec rentmap-server bash -c "cd /app && python scripts/migrate.py status"  # 보류 확인
docker exec rentmap-server bash -c "cd /app && python scripts/migrate.py up"      # 적용
docker restart rentmap-server rentmap-dbtool        # 새 컬럼/테이블 인식
```

### 5.3 dbtool 임시 중단

```sh
docker compose stop rentmap-dbtool
# 본 사이트(rentmap-server)는 영향 없음
```

다시 띄우기: `docker compose up -d rentmap-dbtool`.

### 5.4 감사 로그 보존

`admin_audit_log` 는 day당 ~수십 행 수준이라 별 retention 정책 없이 둡니다. 100k 행 넘으면 90일 sweep 추가 고려.

```sh
docker exec rentmap-postgres psql -U rentmap -d rentmap -c "
SELECT count(*), min(created_at), max(created_at)
  FROM admin_audit_log;"
```

---

## 6. 트러블슈팅

| 증상 | 점검 |
|---|---|
| `rentmap-db.anzam.kr` 가 503 | `docker logs rentmap-dbtool`, Caddy 가 `proxy` 네트워크에 있는지 |
| `Not authenticated` 반복 | 쿠키 도메인 mismatch — Caddy `header_up Host {host}` 가 있는지 |
| `Cross-origin write rejected` | reverse-proxy가 Origin 헤더 누락 — Caddy 기본값에서 통과되지만 일부 클라이언트가 stripped 보낼 때 발생 |
| `refusing to re-apply NNN` | sha drift. §4.2.1과 동일 패턴으로 새 fix-forward 마이그레이션 작성 |
| `region_schedules_source_check` 위반 | 마이그레이션 017 미적용 — `migrate.py status` 확인 |

---

## 7. 보안 노트

* dbtool에 admin 권한이 있다는 건 **DB 전체에 대한 mutate 권한**과 동의어입니다. Caddy 도메인을 인덱싱에 노출하지 말고, IP allowlist나 basic_auth 둘 중 하나는 반드시.
* `reverse_sql` 컬럼에는 텍스트 SQL이 그대로 저장됩니다. 정상 흐름에서는 `psycopg.sql.Literal()` 로 quote 된 텍스트만 들어가지만, 감사 로그 읽기 권한이 있다는 건 모든 변경 이력 + 어떤 SQL이 실행됐는지 볼 수 있다는 뜻 — 누구에게 `is_admin` 을 줄지 신중하게.
* 본 사이트의 admin.html 과 별 쿠키(`rentmap_dbtool_session`)라서 한쪽 로그인이 다른 쪽에 자동 전파되지 않습니다. 의도된 동작.
