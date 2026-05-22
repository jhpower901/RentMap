# 부동산 데이터 크롤링 컬럼 분석 결과

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
