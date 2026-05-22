api, web crawling 등을 이용해 수집한 데이터는 사용자가 웹에서 얻을 수 있는 데이터와 같거나 많아야 함.
## 1. 전체 컬럼 합집합 (Total Columns)
현재 수집된 모든 플랫폼(다방, 당근마켓, 네이버 부동산, 직방) 및 통합본 CSV 파일들에 존재하는 컬럼의 합집합은 **총 43개**입니다.

1. `source` (출처)
2. `listing_no` (매물 번호)
3. `url` (매물 링크)
4. `address` (주소)
5. `latitude` (위도)
6. `longitude` (경도)
7. `title` (매물 제목)
8. `deposit_manwon` (보증금)
9. `rent_manwon` (월세)
10. `maintenance_manwon` (관리비)
11. `total_monthly_manwon` (총 월 고정 비용)
12. `room_type` (방 형태)
13. `area_m2` (면적)
14. `floor` (층수)
15. `approval_date` (사용승인일)
16. `image_1` (이미지 1)
17. `image_2` (이미지 2)
18. `crawl_note` (크롤링 메모)
19. `room_id` (다방, 네이버, 통합본)
20. `item_id` (직방)
21. `agency` (중개사명)
22. `agent_name` (중개인명)
23. `agent_phone` (중개인 연락처)
24. `region` (지역)
25. `address_public_level` (주소 공개 범위)
26. `direction` (방향)
27. `parking` (주차 여부)
28. `move_in` (입주 가능일)
29. `options` (옵션)
30. `building_use` (건축물 용도)
31. `security_options` (보안 옵션)
32. `writer_type` (작성자 타입 - 당근)
33. `region_depth1` (시/도 - 당근)
34. `region_depth2` (시/군/구 - 당근)
35. `region_depth3` (읍/면/동 - 당근)
36. `room_count` (방 개수 - 당근)
37. `realtor_name` (법정 중개인명 - 직방)
38. `realtor_phone` (법정 중개인 연락처 - 직방)
39. `agency_address` (중개사 주소 - 직방)
40. `agency_reg_no` (중개사 등록번호 - 직방)
41. `service_type` (서비스 타입 - 직방)
42. `residence_type` (주거 형태 - 직방)
43. `non_compliant_building` (위반건축물 여부 - 직방)

---

## 2. 모든 파일에 공통으로 존재하는 컬럼 (교집합)
어떤 플랫폼이든 예외 없이 무조건 공통적으로 수집된 컬럼은 **총 18개**입니다.
- `source`, `listing_no`, `url`, `address`, `latitude`, `longitude`, `title`, `deposit_manwon`, `rent_manwon`, `maintenance_manwon`, `total_monthly_manwon`, `room_type`, `area_m2`, `floor`, `approval_date`, `image_1`, `image_2`, `crawl_note`

---

## 3. 파일별 고유 컬럼 (다른 플랫폼 파일과 겹치지 않는 컬럼)

### 🥕 당근마켓 (`daangn_ajou_2026-05-22.csv`)
당근마켓 데이터는 공인중개사나 세부 옵션/방향 정보가 없는 대신, 지역 계층과 작성자 정보가 고유하게 존재합니다.
- `writer_type` (작성자 타입: BROKER 등)
- `region_depth1` (도/시 단위 지역)
- `region_depth2` (구 단위 지역)
- `region_depth3` (동 단위 지역)
- `room_count` (방 개수)

### 🏠 직방 (`zigbang_ajou_2026-05-22.csv`)
직방 데이터는 고유 식별자(`item_id`)를 사용하며, 중개사무소에 대한 상세한 법적/행정적 정보를 포함하고 있습니다.
- `item_id` (직방 전용 식별자)
- `realtor_name` (법정 중개인 이름)
- `realtor_phone` (법정 중개인 연락처)
- `agency_address` (중개사무소 상세 주소)
- `agency_reg_no` (중개사무소 등록번호)
- `service_type` (서비스 타입)
- `residence_type` (상세 주거 형태)
- `non_compliant_building` (위반건축물 여부: True/False)

### 🟦 다방, 네이버 부동산, 통합본
`dabang_...`, `naver_land_...`, `ajou_rentals_combined_...` 이 3개의 파일은 서로 **100% 동일한 컬럼 구조**를 공유합니다. 당근/직방과 비교했을 때 이 파일들에만 있는 고유 컬럼은 다음과 같습니다.
- `room_id` (고유 방 ID)
- `building_use` (건축물 용도)
- `security_options` (보안 옵션)

# Naver Land Example (Truncated)
--------------------------------------------------------------------------------------------------
```html
<div class="detail_contents_inner">
  <div class="photo_area">
    <div class="main_photo_wrap">
      <button class="main_photo_item" style="background-image: url('https://landthumb-phinf.pstatic.net/...jpg');"></button>
    </div>
  </div>
  <div class="main_info_area">
    <div class="info_title_wrap">
      <h4 class="info_title"><strong class="info_title_name">일반원룸 1동</strong>2층</h4>
    </div>
    <div class="info_article_price">
      <span class="type">월세</span><span class="price">500<span class="slash">/</span>50</span>
    </div>
  </div>
  <table class="info_table">
    <tr class="info_table_item">
      <th class="table_th">해당층/총층</th><td class="table_td">2/4층</td>
      <th class="table_th">방향</th><td class="table_td">남향(거실 기준)</td>
    </tr>
    <!-- ... additional rows for area, approval date, etc ... -->
  </table>
  <div class="table_td_agent">
    <div class="info_agent_title"><strong class="info_title">런공인중개사사무소</strong></div>
    <dl class="info_agent">
      <dt class="title">대표</dt><dd class="text">김지연</dd>
      <dt class="title">전화</dt><dd class="text text--number">031-213-5888</dd>
    </dl>
  </div>
</div>
```
--------------------------------------------------------------------------------------------------

# Daangn Example (Truncated)
--------------------------------------------------------------------------------------------------
```html
<div class="md:w-[390px]">
  <h1 class="t5-bold">수원시 영통구 원천동</h1>
  <div class="mt-x4">
    <span class="t2-bold">월세 500 / 33</span>
    <span class="t4-medium">관리비 7만</span>
  </div>
  <p class="article-body">
    수원 나누리병원에서 1분거리에 위치한 원룸입니다... 매우 조용하고 버스정류장도 근처에 있어 편했습니다!
  </p>
  <div class="flex gap-x2">
    <span class="seed-badge">중개사</span>
    <span class="seed-badge">오픈형 원룸</span>
    <span class="seed-badge">19.83㎡</span>
  </div>
  <div class="mt-x5">
    <div class="t4-medium">16일 전 · 채팅 2 · 관심 3 · 조회 156</div>
  </div>
</div>
```
--------------------------------------------------------------------------------------------------

# Dabang Example (Truncated)
--------------------------------------------------------------------------------------------------
```html
<section class="diLgCh">
  <header><h1>매물 57143971</h1></header>
  <div class="kWdYAe">
    <img src="https://d1774jszgerdmk.cloudfront.net/..." alt="이미지">
  </div>
  <div class="sc-egHnuI">
    <p>🔵 금액 ( 보증금/월세/관리비 ) : 500/48/10</p>
    <p>🔵 구조 : 분리형 원룸</p>
    <p>✔ 아주대 , 아주대삼거리 5분</p>
  </div>
  <section class="ghMCwP">
    <h1>중개사무소 정보</h1>
    <h1>라인공인중개사사무소</h1>
    <ul>
      <li><h1>주소</h1><p>경기도 수원시 팔달구 아주로 17-23 101호(우만동)</p></li>
      <li><h1>대표명</h1><p>임희주</p></li>
      <li><h1>중개등록번호</h1><p>41115-2024-00046</p></li>
    </ul>
  </section>
</section>
```
--------------------------------------------------------------------------------------------------

# Zigbang Example (Truncated)
--------------------------------------------------------------------------------------------------
```html
<div class="kOcHzF">
  <div data-testid="원룸매물목록툴바">
    <div class="css-1563yu1">지역 목록 5개</div>
  </div>
  <div class="css-1dbjc4n">
    <div class="css-901oao">월세 500/45</div>
    <div class="css-901oao">원룸 · 19.83㎡ · 3층</div>
    <div class="css-1563yu1">🩵아주대 , 병원 인근 바로 앞🩵</div>
  </div>
  <div class="css-1dbjc4n">
    <img src="https://resource.zigbang.com/profile/..." alt="프로필">
    <div class="css-901oao">행운공인중개사사무소</div>
    <div class="css-901oao">대표 김행운</div>
  </div>
</div>
```
--------------------------------------------------------------------------------------------------
