# web — 바로가 1차 시각화 (FastAPI + Leaflet)

모든 명령은 **저장소 루트에서 PowerShell** 로 실행한다. 파이썬은 항상 `.\.venv\Scripts\python.exe` 를 쓴다
(전역 `python` 은 msys2 판이라 의존성이 설치되지 않는다).

## 1. 설치

```powershell
.\.venv\Scripts\python.exe -m pip install -r data\requirements.txt -r web\requirements.txt pytest
```

Leaflet 1.9.4 는 `web/frontend/vendor/leaflet-1.9.4/` 에 들어 있다. 빌드 단계는 없다.

## 2. 키 — 저장소 루트 `.env`

| 변수 | 필요 | 용도 |
|---|---|---|
| `KAKAO_REST_API_KEY` | 경로·장소 검색 | 카카오맵 대중교통 · 카카오모빌리티 자동차 길찾기 · 카카오 로컬 키워드/주소 검색 |
| `VWORLD_API_KEY` | 배경지도 | VWorld 타일 프록시 (없으면 OpenStreetMap 으로 바꿔 쓴다) |
| `KAKAO_TRANSIT_DAILY_LIMIT` | 선택, 기본 1000 | 대중교통 일일 호출 한도 |
| `KAKAO_CAR_DAILY_LIMIT` | 선택, 기본 10000 | 자동차 일일 호출 한도 |
| `KAKAO_KEYWORD_DAILY_LIMIT` | 선택, 기본 100000 | 키워드로 장소 검색 일일 호출 한도 |
| `KAKAO_ADDRESS_DAILY_LIMIT` | 선택, 기본 100000 | 주소 검색 일일 호출 한도 |

기본 한도는 카카오 무료 쿼터와 같다 — 넘기 전에 서버가 막으므로 초과 과금이 생기지 않는다.
카카오맵 [사용 설정] 하나가 대중교통과 로컬 검색을 함께 연다.

- `KEY ="value"` 형식도 읽는다. 이미 설정된 환경변수가 `.env` 보다 우선한다. `.env` 는 커밋하지 않는다(gitignore).
- 키는 서버에만 있다. 브라우저는 `/api/*`·`/tiles/*` 만 부르고, `/api/health` 는 키가 있는지 여부만 알려 준다.

## 3. 데이터

정류소·역·행정경계(`data/processed/`)가 있어야 지도에 정류소가 뜬다. 파이프라인 명령은 [`data/README.md`](../data/README.md) 를 따른다.
산출물이 없어도 서버는 뜬다. 이때 `/api/health` 는 `ready: false` 를 주고, 정류소·경계 API 는 `data_not_built`(503) 를 준다.
버스 노선 순서표(`data/processed/bus_route_stops.csv`)가 있으면 버스 정류장을 눌렀을 때 경유 노선이 보이고, 노선을 누르면 그 노선의 정류장이 지도에 강조된다(없어도 나머지는 동작).
정류소 CSV·노선 순서표나 `data/ref/line_groups.csv` 를 고친 뒤에는 서버를 다시 시작한다 (시작할 때 한 번 읽는다).

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
| `GET /api/transit?sx&sy&ex&ey&probe=0` | 대중교통 경로 + 승·하차 정류장 매칭 진단 (`probe=1` 은 응답 구조 요약 추가) |
| `GET /api/car?sx&sy&ex&ey` | 자동차 경로·택시 요금 |
| `GET /api/search?q=` | 주소·장소 이름 → 출발·도착 후보 (키워드·주소 검색을 함께 불러 번지 주소 → 장소 → 지역 순. `q` 는 100자까지. 한쪽만 실패하면 `failed` 에 적고 다른 쪽 결과를 준다) |
| `GET /api/quota` | 오늘 사용량/한도 |
| `GET /tiles/vworld/{layer}/{z}/{y}/{x}` | VWorld 타일 프록시 (`Base`, `white`, `midnight`, `Hybrid`, `Satellite`; z 6–19, 단 `white`·`midnight` 는 z18 까지만. 프론트는 `Base`(밝은 화면) · `midnight`(다크 모드, z19 에서는 z18 을 확대)만 쓴다) |

오류는 `{"error": {"code", "message", "action", "upstream_status"}}` 형태로 온다.

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
- 테스트 픽스처는 문서 스키마를 보고 손으로 쓴 합성 데이터다(`"_note": "SYNTHETIC ..."`). 실제 응답을 픽스처로 저장하지 않는다.
- 경로를 표시할 때 `© Kakao, Kakao Mobility` 를 표기한다.
