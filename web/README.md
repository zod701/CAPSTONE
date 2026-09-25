# web — 바로가 1차 시각화 (FastAPI + Leaflet)

모든 명령은 **저장소 루트에서 PowerShell** 로 실행한다. 파이썬은 항상 `.\.venv\Scripts\python.exe` 를 쓴다
(전역 `python` 은 msys2 판이라 의존성이 설치되지 않는다).

## 1. 설치

```powershell
.\.venv\Scripts\python.exe -m pip install -r data\requirements.txt -r web\requirements.txt pytest
```

Leaflet 1.9.4 는 `web/frontend/vendor/leaflet-1.9.4/` 에 들어 있다. 빌드 단계는 없다.

### 현재 위치와 모바일 UI

- 지도 왼쪽의 **GPS** 버튼을 누르고 위치 권한을 허용하면 위치 점과 정확도 원을 표시한다.
  최초 수신 시 현재 위치로 이동하고 출발·도착 팝업을 연다. 위치 점을 다시 눌러도 같은 메뉴를 연다.
- 이동 시 위치 점만 갱신한다. 이미 선택한 출발·도착과 경로는 자동으로 변경하지 않는다.
  GPS를 다시 누르면 최신 위치로 확대하고 출발·도착 팝업을 다시 연다. 버튼으로 추적을 취소하지 않는다.
  화면을 백그라운드로 전환하면 추적을 중지한다. 복귀 후 GPS를 눌러 재시작한다.
- 추적 자체는 브라우저 Geolocation API를 사용하며 경로·주소 API를 자동 호출하지 않는다.
  최초 지도 이동에는 기존 지도 타일·정류장 조회가 발생할 수 있다.
- 위치 권한과 보안 연결이 필요하다. 휴대폰에서 접속할 때는 **HTTPS**를 사용한다.
  개발 PC의 `localhost`/`127.0.0.1`은 사용할 수 있지만 휴대폰에서 접속하는 일반 HTTP LAN 주소는 사용할 수 없다.
- 720px 이하의 기본 화면은 전체 지도와 상단 플로팅 검색바다. 레이어 목록은 기본적으로 접힌다.
  첫 접속에는 패널을 접고, 출발지나 도착지를 처음 지정하면 자동으로 연다(데스크톱도 동일).
  모바일에서는 출발·도착과 경로 검색 버튼에 맞춰 작게 열린다. 손잡이를 위로 드래그하면 절반 높이,
  누르면 닫힌다. 닫힌 손잡이를 누르면 다시 작은 패널을 연다.
- 모바일 경로 검색 중·결과 목록은 전체화면이다. 출발·도착, 유형 탭, 정렬은 고정하고 카드 목록만 스크롤한다.
  경로 카드를 선택하면 지도와 카드 패널을 절반씩 보여준다. 손잡이로 위쪽은 전체 패널,
  아래쪽은 전체 패널 → 절반 → 지도 전체화면 순으로 전환한다. 검색창은 패널 밖 상단에 유지한다.
- 데스크톱도 전체 지도 위 왼쪽 상단에 검색바를 띄운다. 왼쪽 중앙 핸들을 클릭하거나
  오른쪽으로 드래그하면 플로팅 패널이 열린다. 열린 핸들을 클릭하거나 반대로 드래그하면 닫힌다.
- 위치 추적 자동 테스트: `node --test web/tests/location.test.mjs` (추가 패키지 불필요).

하이브리드의 잘린 대중교통 요금은 [공개 성인 교통카드 규칙](../fare_rules.md)으로 추정한다.
응답의 `fare_info`에 대중교통 금액(`value`)·출처(`rules_estimate`/`fallback`)·사유(`reason`)를 담고 화면에 표시한다.
새 대중교통 API 경로 요금을 그대로 쓰는 A·(b) B는 `fare_info=null`이다.

## 2. 키 — 저장소 루트 `.env`

| 변수 | 필요 | 용도 |
|---|---|---|
| `KAKAO_REST_API_KEY` | 경로·장소 검색 | 카카오맵 대중교통 · 카카오모빌리티 자동차 길찾기 · 카카오 로컬 키워드/주소 검색 |
| `VWORLD_API_KEY` | 배경지도 | VWorld 타일 프록시 (없으면 OpenStreetMap 으로 바꿔 쓴다) |
| `KAKAO_TRANSIT_DAILY_LIMIT` | 선택, 기본 1000 | 대중교통 일일 호출 한도 |
| `KAKAO_CAR_DAILY_LIMIT` | 선택, 기본 10000 | 자동차 일일 호출 한도 |
| `KAKAO_KEYWORD_DAILY_LIMIT` | 선택, 기본 100000 | 키워드로 장소 검색 일일 호출 한도 |
| `KAKAO_ADDRESS_DAILY_LIMIT` | 선택, 기본 100000 | 주소 검색 일일 호출 한도 |
| `DATA_GO_KR_API_KEY` | 버스 실시간 도착 | 경기 GBIS · 서울 TOPIS 버스 도착정보 (두 원천이 같은 키를 쓴다) |
| `SEOUL_SUBWAY_LIVE_API` | 도시철도 실시간 도착 | 서울 열린데이터광장 '실시간 지하철' (서울 밖도 된다. 용인에버라인·의정부경전철·김포골드라인·인천 1·2호선은 제공하지 않는다) |
| `GYEONGGI_BUS_DAILY_LIMIT` | 선택, 기본 1000 | 경기 버스 도착정보 일일 호출 한도 |
| `SEOUL_BUS_DAILY_LIMIT` | 선택, 기본 1000 | 서울 버스 도착정보 일일 호출 한도 |
| `SEOUL_SUBWAY_DAILY_LIMIT` | 선택, 기본 1000 | 도시철도 실시간 도착 일일 호출 한도 |

기본 한도는 카카오 무료 쿼터와 같다 — 넘기 전에 서버가 막으므로 초과 과금이 생기지 않는다.
도착정보 세 원천의 기본 1000건/일은 공공데이터 개발계정 한도와 같다 (data.go.kr 은 활용신청이 승인되어야 응답한다).
카카오맵 [사용 설정] 하나가 대중교통과 로컬 검색을 함께 연다.

- `KEY ="value"` 형식도 읽는다. 이미 설정된 환경변수가 `.env` 보다 우선한다. `.env` 는 커밋하지 않는다(gitignore).
- 키는 서버에만 있다. 브라우저는 `/api/*`·`/tiles/*` 만 부르고, `/api/health` 는 키가 있는지 여부만 알려 준다.

## 3. 데이터

정류소·역·행정경계(`data/processed/`)가 있어야 지도에 정류소가 뜬다. 파이프라인 명령은 [`data/README.md`](../data/README.md) 를 따른다.
산출물이 없어도 서버는 뜬다. 이때 `/api/health` 는 `ready: false` 를 주고, 정류소·경계 API 는 `data_not_built`(503) 를 준다.
버스 노선 순서표(`data/processed/bus_route_stops.csv`)가 있으면 버스 정류장을 눌렀을 때 경유 노선이 보이고, 노선을 누르면 그 노선의 정류장이 지도에 강조된다(없어도 나머지는 동작).
경로 매칭도 이 표를 쓴다 — 버스 구간의 진행 방향(상·하행)을 운행 순서로 가리고, 경유 정류소는 순서표에서 그대로 읽는다(없으면 이름·거리로 찾는다).
승·하차 이름이 어긋나도 이 표로 구제한다: 토막 순서만 뒤바뀐 복합 이름(`A.B` ↔ `B.A`)은 토막 집합으로 맞추고(진단 `match_level: parts`),
쓸 이름 후보가 **하나도 없으면**(글자가 맞은 `key`·`alias`·`parts` 후보가 없고, 앞 4글자만 같은 `prefix` 후보도 그 노선에 서지 않으면) 이름을 버리고 그 노선이 서는 정류장 중 **운행 순번 간격이 구간 정류소 개수와 정확히 맞는** 곳을 고른다(`match_level: route`, `line_filter: route`).
간격으로 확인되지 않으면 고르지 않는다(`line_filter: route_gap` → 미매칭). 글자가 맞는 행이 있는데 그 행이 순서표에 없으면 **순서표가 낡은 것**이므로 구제하지 않고 그 역할만 노선으로 가리지 않는다(`line_filter: mismatch`) — 글자가 맞은 이름 근거를 노선 순서와 바꾸지 않는다. 간격 제한은 역할마다 따로 보므로 한쪽 끝이 약해도 다른 쪽 끝의 진행 방향 판정은 그대로다.
이름 글자가 다른 채 고른 항목은 `name_mismatch: true` 와 `rescue`(노선 id·간격·경쟁 후보 수 `n_rivals`)·`nearest_any` 를 함께 주고, 경쟁 후보가 남으면 거리 차가 크더라도 `ambiguous` 다 — 정류소 DB 가 낡았다(신설·개명)는 신호를 구제가 삼키지 않게.
순번이 한 칸 붙은 경쟁 후보는 모호 근거로만 쓰고 `chosen` 으로 고르지 않는다(지도 칩·실시간 도착이 `chosen` 을 쓴다) — 그래서 `second_gap_m` 은 경쟁 후보가 더 가까우면 음수다.
승·하차를 양쪽 다 노선으로 가렸는데 **한 노선으로 이어지지 않으면**(같은 이름의 노선이 여럿일 때) 둘 다 `ambiguous`·`line_filter: unlinked` 로 내린다 — 둘 중 하나는 반드시 오답이다.
진단 화면은 `match_level` 5개(`key`·`alias`·`parts`·`prefix`·`route`)를 모두 그려야 하고, `nearest_any`·`nearest_same_name_m` 는 `chosen` 이 있는 `parts`·`route` 항목에도 채워진다 — 그 두 값이 구제가 삼킨 '낡은 DB' 신호다.
도시철도도 같다: 역 순서표(`data/processed/subway_line_seq.csv`)가 있으면 구간의 역을 거기서 읽는다(급행처럼 역 개수가 안 맞으면 이름·거리로 찾는다).
역 실시간 도착 매핑표(`data/processed/subway_live_stations.csv`)는 우리 역 id 를 실시간 API 의 역명·노선(subwayId)에 잇는다 — 실시간 API 는 역명 정확 일치만 받고 옛 이름을 쓰는 역이 있다(DB '능길' ↔ 실시간 '신길온천'). 이 표가 없으면 `/api/arrivals/station/…` 만 `data_not_built`(503) 가 되고, 표에 실시간 이름이 없는 역은 `no_realtime`(404) 이다.
대중교통 경로에는 **차를 기다리는 시간**을 더한 값도 함께 준다 — 카카오 소요 시간에는 대기가 전혀 없다(차내 시간 + 환승 도보 + 양 끝 도보뿐).
배차간격 표(`data/processed/headway_bus.csv` · `headway_rail.csv`)로 확인하지 못한 승차 구간은 기본 대기 900초를 적용한다.
표 자체가 없어도 같은 기본값을 쓰며, `wait_source=default`와 화면의 **실제 배차 미반영 · 기본 대기 15분 적용** 표시로 알린다. 실제 대기 0초는 유지한다.
정류소 CSV·노선 순서표·실시간 역 매핑표·배차간격 표나 `data/ref/line_groups.csv` 를 고친 뒤에는 서버를 다시 시작한다 (시작할 때 한 번 읽는다).

## 4. 실행

```powershell
$env:PYTHONUTF8="1"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir web/backend --host 127.0.0.1 --port 8000 --reload --reload-dir web/backend
```

브라우저에서 <http://127.0.0.1:8000/> 을 연다.

- **워커는 1개만** 쓴다 (`--workers` 금지). 쿼터 잠금이 프로세스 안에서만 유효하다.
- 쿼터 카운트는 `web/backend/var/quota.json` 에 KST 날짜별 건수만 남는다(gitignore). 한국 시간 자정(00:00)에 초기화된다.

| 엔드포인트 | 내용 |
|---|---|
| `GET /api/health` | 준비 여부, 정류장·역 수, 데이터 시각, 키 유무(불리언) |
| `GET /api/stops?bbox=minLon,minLat,maxLon,maxLat&kinds=bus,subway&virtual=0&limit=5000` | 범위 안 정류장·역 (`bus` 는 `bbox` 필수, `limit` 은 종류별 최대 10000, 넘치면 `truncated`) |
| `GET /api/stops/{정류장키}/routes` | 버스 정류장을 지나는 노선 (미정차 제외, 이름 순). `data/processed/bus_route_stops.csv` 가 없으면 `data_not_built`(503) |
| `GET /api/routes/{노선ID}` | 노선이 지나는 정류장 (운행 순서, 미정차 제외, 같은 정류장은 한 번) — 지도에서 정류장만 강조한다 |
| `GET /api/boundaries` | 시군구 경계 GeoJSON (서울 25구 + 경기 31시·군) |
| `GET /api/transit?sx&sy&ex&ey&probe=0` | 대중교통 경로 + 승·하차 정류장 매칭 진단 + 대기시간(첫 승차는 실시간 도착정보, 나머지는 배차로 추정) (`probe=1` 은 응답 구조 요약 추가) |
| `GET /api/car?sx&sy&ex&ey` | 자동차 경로·택시 요금 |
| `GET /api/hybrid?sx&sy&ex&ey&top=5&t_max=30` | 대중교통+택시 연계 경로 (선호는 시간 중시로 고정 — 택시를 섞는 사람은 돈보다 시간이 급하다. 앵커 후보를 `algo` 가 만들고 상위 `top` 개만 카카오로 확인한다. **쿼터를 크게 쓴다** — 기준 경로 1콜 + 앵커마다 자동차 1콜, 대중교통은 A 유형과 (b) 역추적이 낸 B 유형에 1콜씩 더. 중간 공백을 메우는 D 는 자동차 1콜뿐. 돌아가는 구간에서 내리는 C 는 예산을 따로 둬 대중교통 최대 6 · 자동차 최대 8콜 — `top=5` 면 모두 합쳐 최대 25콜) |
| `POST /api/hybrid?sx&sy&ex&ey&top&t_max` | 같은 출발·도착의 `/api/transit` 결과를 본문 `{"routes": [...]}` 로 받아 기준 경로로 다시 쓴다(대중교통 1콜 절약 — 최대 24콜). 첫 승차 실시간 대기는 배차 추정으로 되돌린 뒤 요청 시각으로 다시 매긴다. 본문이 올바르지 않으면 `invalid_request`(400). `diag.base_source` 가 `reused`(다시 씀)·`called`(새로 부름) |
| `GET /api/search?q=` | 주소·장소 이름 → 출발·도착 후보 (키워드·주소 검색을 함께 불러 번지 주소 → 장소 → 지역 순. `q` 는 100자까지. 한쪽만 실패하면 `failed` 에 적고 다른 쪽 결과를 준다) |
| `GET /api/arrivals/stop/{정류장키}` | 버스 정류장 실시간 도착 (그 정류장을 등록한 BIS 전부를 함께 부르고 도착 임박 순으로 합친다. 한쪽만 실패하면 `failed` 에 적고 다른 쪽 결과를 준다. 미정차·BIS 없는 정류장은 `no_realtime`(404)) |
| `GET /api/arrivals/station/{역ID}` | 도시철도 역 실시간 도착 (매핑표의 실시간 역명으로 부르고 그 역의 노선만 남긴다. 원천이 하나라 `failed` 는 항상 빈 목록) |
| `GET /api/quota` | 오늘 사용량/한도 |
| `GET /tiles/vworld/{layer}/{z}/{y}/{x}` | VWorld 타일 프록시 (`Base`, `white`, `midnight`, `Hybrid`, `Satellite`; z 6–19, 단 `white`·`midnight` 는 z18 까지만. 프론트는 `Base`(밝은 화면) · `midnight`(다크 모드, z19 에서는 z18 을 확대)만 쓴다) |

오류는 `{"error": {"code", "message", "action", "upstream_status"}}` 형태로 온다.

하이브리드(`/api/hybrid`)는 **화면의 버튼을 눌렀을 때만** 부른다 — `top=5` 면 대중교통 최대 6콜 · 자동차 5콜이다.
응답의 `routes[]` 는 `hybrid`(A = 택시 → 대중교통, B = 대중교통 → 택시, D = 기준 경로 한가운데 공백 한 구간만 택시,
C = 기준 경로가 목적지에서 멀어지기 시작하는 정류장에서 내려 택시로 목적지까지 또는 곧장 가는 노선의 앵커로) ·
`detour`(C 만 — `via` 가 `taxi_to_d`·`reanchor`, 내린 정류장 `point_name`, 돌아가는 비용 `loop_s`, `transit.steps` 에서 택시를 끼울 자리 `taxi_at`) ·
`anchor`(갈아타는 정류장·역, D 는 택시 출발점 `from_lat`·`from_lon` 도) · `gap`(D 만 — 공백 종류 `wait`·`walk`, 뺀 시간, 다시 차를 타는지,
`transit.steps` 에서 뺀 자리 `at`) · `taxi_counted_s`(총 시간에 넣은 택시 몫 — 호출 대기 포함, 택시 뒤에 차를 잡아야 하면 연결 여유도) ·
`taxi`(자동차 경로 응답) · `transit`(A 와 (b) 역추적이 낸 B 는 따로 부른 경로, (a) 섭동이 낸 B 는 기준 경로를 앵커에서 자른 것,
D 는 기준 경로에서 택시가 대신한 한 구간만 뺀 것) ·
`time_s`·`fare`·`gc_s`·`won_per_min`·`recommended`·`pareto` 를 담고, `baseline` 이 기준선 — 받은 대중교통 경로 중 하이브리드와 같은 잣대(차내 + 첫·끝 도보 추정 + 대기)로 잰 최소 시간 경로다
(5분 기준 걸러내기 · 원/분 · 추천 · Pareto 에 쓰고, 화면 카드의 "N분 절약 · N원 추가 · 원/분" 도 이것과 견준다).
**첫 승차 대기는 실시간 도착정보로** 바꾼 기준 경로(`with_realtime_first_wait`)를 기준선 · D · (a) 섭동의 B 에 쓴다 — 첫 승차
정류장·역마다 도착정보 1콜(공공데이터 쿼터, 15초 캐시)이고, 없거나 실패하면 정적 배차 대기로 남는다(`diag.realtime` 에 사유별 건수).
(a) 섭동의 A 만 원래 경로를 쓴다 — 택시로 정류장에 닿아 '걸어서 닿는다' 는 전제가 맞지 않는다. 그래서 (a) 는 A·B 를 나눠 두 번
부른다(`diag.perturb_a` · `perturb_b`). 실시간으로 바꾼 구간은
`wait_source`(`realtime` 도착 예측 초 · `realtime_stops` 남은 정류장·역 수로 어림 · `realtime+headway` 알려진 차를 다 놓쳐
배차간격으로 이음)와 `static_wait_s`(배차로만 추정한 값) · `walk_to_stop_s` 를 함께 단다.
[경로 검색] 의 `/api/transit` 도 같은 방식으로 첫 승차 대기를 실시간으로 바꾼다(응답 `realtime` 에 사유별 건수) —
출발지에서 걸어서 첫 정류장에 닿는 경로라 전제가 맞는다. 화면은 실시간으로 잡은 대기를 초록 '실시간' 으로 따로 보이고,
마우스를 올리면 세 방식 중 무엇으로 잡았는지 밝힌다.
검증한 뒤 **기준선보다 5분 이상 빠르지 않은 후보는 뺀다**(`compare.saves_enough`, `diag.too_little_saving` 에 뺀 수).
후보 생성과 병합(`merge_candidates` — D 는 택시 양끝을 모두 비교한다)은 저장소 루트의 `algo` 패키지가 한다 —
없으면 이 엔드포인트만 `hybrid_unavailable`(503) 이다.

대기시간(`/api/transit`)은 구간마다 `step.headway_m`(배차 분)·`step.wait_s`, 경로마다 `route.wait_s`·`route.total_with_wait_s`,
응답 맨 위에 `wait_basis{day_type, hour}` 로 온다. 사람이 아무 때나 온다고 보고 **대기 = 배차 ÷ 2** 이고, 한 구간에서 탈 수 있는
노선이 여럿이면 빈도를 더한다(유효 배차 = 1 / Σ(1/hᵢ)). 이름이 같은 노선 후보(매칭이 하나로 좁히지 못한 경우)는 한 노선으로 보고
평균을 쓴다. 배차를 모르는 승차 구간마다 기본 대기 900초(15분)를 더하며 `step.wait_source=default` 로 표시한다.
화면에는 ‘실제 배차 미반영 · 기본 대기 15분 적용’을 표시한다. 배차표가 없어도 같은 기본값으로 합산하며, 실제 대기 0초는 그대로 유지한다.
버스는 노선 × 요일 유형(그날 값이 없으면 평일), 도시철도는 역 × 노선군 × 시간대(방향 평균, **평일 시각표** 기준)에서 읽는다.

도착정보의 `eta_s` 는 **기다릴 초**다. 도시철도 예측(`barvlDt`)은 원천이 값을 받은 시각 기준이라 그만큼 빼고(실측 경과 41~281초,
원천 시계가 앞서 음수로 오는 경우도 있다), 빼서 음수면 예측이 지난 것이니 `null`(모름)로 둔다. 예측이 0 으로 오는 행은
도착 코드로 가른다 — 진입·도착이면 0, 출발·운행중이면 `null` 이다(그대로 0 으로 담으면 20역 떨어진 열차가 '지금 도착'이 된다).
`age_s` 로 수신 경과를 함께 주므로 오래된 행은 버리면 된다. 예측이 없는 항목은 남은 역·정류소 수 순으로 뒤에 붙는다.

카카오 응답은 캐시가 금지지만(§6), 공공데이터 도착정보는 저장이 허용되어 같은 (원천, 정류장·역) 요청을 **15초만 메모리에** 둔다 —
여러 화면이 같은 정류장을 함께 볼 때 쿼터를 아낀다. 캐시 히트는 호출 건수를 늘리지 않고, 디스크에 쓰는 것은 여전히 건수(`quota.json`)뿐이다.

## 5. 테스트

```powershell
$env:PYTHONUTF8="1"
.\.venv\Scripts\python.exe -m pytest web/tests
```

손으로 쓴 합성 픽스처와 `httpx.MockTransport` 만 쓴다. 카카오·VWorld 를 부르지 않고 `.env` 도 읽지 않는다.

## 6. 카카오 약관

- 카카오 응답은 요청을 처리하는 동안 메모리에만 둔다. 디스크·로그·캐시·브라우저 저장소(localStorage 등)에 남기지 않는다.
  로컬 검색 결과는 짧은 캐시도 금지라, 같은 검색어를 다시 쳐도 다시 부른다 (브라우저는 목록을 그리는 동안만 들고 있다).
- 카카오에서 온 응답에는 `Cache-Control: no-store` 를 붙인다. 디스크에 쓰는 것은 호출 건수(`quota.json`)뿐이다.
- 하이브리드는 화면이 방금 받은 [경로 검색] 결과를 요청 본문으로 돌려받아 기준 경로로 쓴다. 서버는 그 요청을 처리하는 동안에만 쓰고 어디에도 두지 않으며, 화면은 결과를 받은 지 5분 안이고 출발·도착이 그대로일 때만 보낸다(그 밖에는 새로 부른다).
  카카오 공식 답변(devtalk 151435)은 "저장하여 사용하는 행위는 허용하지 않으며 실시간(라이브) 호출로만 이용" 이라는 원칙만 있고 같은 화면 안에서 받은 결과를 다시 쓰는 경우는 따로 언급하지 않는다 — 보관하지 않고 짧은 시간 안에만 쓰는 쪽으로 해석했다.
- 테스트 픽스처는 문서 스키마를 보고 손으로 쓴 합성 데이터다(`"_note": "SYNTHETIC ..."`). 실제 응답을 픽스처로 저장하지 않는다.
- 경로를 표시할 때 `© Kakao, Kakao Mobility` 를 표기한다.

## 7. 배포 (Vercel)

- 진입점: 저장소 루트 `index.py` 의 `app` (`pyproject.toml` 의 `tool.vercel.entrypoint`). 의존성도 `pyproject.toml` 에 있다(Python 3.13).
- 프론트(`web/frontend`)는 `StaticFiles` 마운트라 빌드 때 CDN 으로 올라가고, `/api`·`/tiles` 만 함수가 받는다.
- 함수 리전은 서울(`icn1`, `vercel.json`) — 공공데이터·VWorld API 가 해외 IP 를 막을 수 있고, 카카오까지 지연도 줄인다.
- Vercel 은 git 에서 빌드하므로 서버가 읽는 `data/processed/` 8개 파일은 저장소에 둔다(`.gitignore` 예외). 데이터를 다시 만들면 함께 커밋한다.
- 키는 `.env` 대신 Vercel 프로젝트 환경 변수로 넣는다: `KAKAO_REST_API_KEY`, `VWORLD_API_KEY`, `DATA_GO_KR_API_KEY`, `SEOUL_SUBWAY_LIVE_API` (한도 변수는 선택).
- **쿼터 카운트는 근사치다**: 함수는 `/tmp` 에만 쓸 수 있어(`VERCEL` 환경 변수가 있으면 `/tmp/baroga/quota.json`) 인스턴스마다 따로 세고 새로 뜨면 0 부터 센다. 실제 한도는 카카오가 막고(-10 → 그날 소진 처리), 정확한 공유 카운트가 필요하면 외부 저장소(예: Upstash Redis)로 옮겨야 한다.
