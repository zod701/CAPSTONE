# data/ — 정제 파이프라인

서울·경기 버스 정류소와 도시철도역을 **좌표 + 행정경계**로 정제해 `processed/` 에 만든다.
웹 백엔드(`web/`)는 `processed/` 와 `ref/` 만 읽는다.

## 폴더

| 폴더 | 내용 | git |
|---|---|---|
| `csv/` | 원본 — 받은 그대로 두고 **수정하지 않는다**. 아래 출처표에서 받아 넣는다 | 무시 |
| `external/` | 스크립트가 내려받은 원본 | 무시 |
| `ref/` | 손으로 작성하는 참조표 (노선 매핑, 역 단위 노선군 보정, 노선군 색·카카오 이름, 버스 쌍 수동 판정, 최신 정류소 파일 좌표가 틀린 버스 정류소의 좌표 `bus_coord_overrides.csv`, 원천에 없는 GTX-A 역 `gtxa_stations.csv`, 다른 역 좌표가 들어간 역의 좌표 `subway_coord_overrides.csv`, 서지 않는 노선으로 잘못 들어간 역 행 `subway_station_drops.csv`, 역 순서표용 노선정보 행 선택 `subway_seq_lines.csv`·보정 `subway_seq_fixes.csv`, 개명으로 이름이 다른 GTFS 역 `gtfs_station_map.csv`) | 커밋 |
| `scripts/` | 파이프라인 스크립트 + 공용 모듈 (`textnorm`, `geoutil`, `regions`) | 커밋 |
| `processed/` | 산출물 — 아래 명령으로 언제든 다시 만든다 | 무시 |
| `reports/` | 단계별 건수 보고서·검토 CSV | 커밋 |
| `tests/` | pytest (합성 데이터만) | 커밋 |

`csv/버스정류장_서울경기_20251031.csv` 는 도시명으로 거른 예전 추출본이다. 도시명과 실제 위치가
어긋나는 행이 있어 **파이프라인 입력이 아니다** (전국 원본 + 행정경계로 지역을 판정한다).

## 실행 (저장소 루트, PowerShell)

전역 `python` 은 쓰지 않는다 (msys2 3.12, pip 없음). 항상 `.venv` 의 파이썬을 쓴다.

```powershell
.\.venv\Scripts\python.exe -m pip install -r data\requirements.txt pytest   # 처음 한 번
$env:PYTHONUTF8="1"
.\.venv\Scripts\python.exe data\scripts\fetch_boundary.py         # → external/admdongkor/ (있으면 건너뜀, --force 로 다시 받기)
.\.venv\Scripts\python.exe data\scripts\build_boundaries.py       # → processed/admin_sgg.geojson, admin_sgg_web.geojson, reports/boundaries.md
.\.venv\Scripts\python.exe data\scripts\fetch_gbis.py             # → external/gbis/<버전>/ 노선·노선–정류소·정류소 파일 (키 DATA_GO_KR_API_KEY, 1콜)
.\.venv\Scripts\python.exe data\scripts\fetch_seoul_routes.py     # → external/seoul_bus/<날짜>/busRouteList.csv 서울 노선 목록·유형 (같은 키, 1콜)
.\.venv\Scripts\python.exe data\scripts\clean_bus_stops.py        # → processed/bus_stops.csv, reports/bus_stops.md 외
.\.venv\Scripts\python.exe data\scripts\clean_subway_stations.py  # → processed/subway_stations.csv, reports/subway_stations.md
.\.venv\Scripts\python.exe data\scripts\build_subway_seq.py       # → processed/subway_line_seq.csv, reports/subway_line_seq.md
.\.venv\Scripts\python.exe data\scripts\build_bus_route_seq.py    # → processed/bus_route_stops.csv, reports/bus_route_stops.md
.\.venv\Scripts\python.exe data\scripts\build_headway.py          # → processed/headway_bus.csv, headway_rail.csv, reports/headway.md
.\.venv\Scripts\python.exe data\scripts\build_subway_live_map.py  # → processed/subway_live_stations.csv, reports/subway_live_map.md
.\.venv\Scripts\python.exe -m pytest data/tests
```

순서대로 실행한다 — 정류소·역 정제는 `processed/admin_sgg.geojson` 으로 시군을 판정하고, 역 순서표는 `processed/subway_stations.csv` 에 역을,
버스 노선 순서표는 `processed/bus_stops.csv` 에 정류소를 붙인다.

**버스 정류소**(`bus_stops.csv`): 전국 원본은 연 1회 갱신이라, 서울·경기 BIS 레코드는 두 BIS 의 최신 정류소 파일(경기 GBIS 정류소,
서울 OA-15067)로 바꾼 뒤 정제한다. 최신 파일에서 빠진 레코드는 폐지로 보고 뺀다 — 단 서울 파일은 서울 소재 정류소만 담으므로 서울 BIS 가
등록한 서울 밖 정류소는 전국 원본 것을 둔다. 최신 파일 좌표가 틀린 곳은 `ref/bus_coord_overrides.csv` 에 한 줄씩 적는다(대상 레코드가
없거나 이름이 다르면 멈춘다). 노선 순서표 보고서의 "300 m 넘게 다른 정류소" 가 새 좌표 오류를 찾는 곳이다. 한강버스 선착장·노선은 뺀다.

**버스 노선 순서표**(`bus_route_stops.csv`): 한 행 = 노선의 정류소 하나. 서울 노선(OA-1095)과 경기 노선(GBIS)을 합쳤다 — 두 원천의
노선은 겹치지 않는다. `seq` 는 원천 순번 그대로의 운행 순서라 같은 노선에서 작을수록 상류다(대상 밖 정류소를 빼서 비는 번호가 있다).
경기는 `updown` 이 상행 → 하행으로 한 순번열에 이어지고 첫 하행이 회차 정류소, 서울은 회차를 지나 한 줄로 이어지며 `updown` 이 없다.
`in_db=0` 은 정류소 DB 에 없는 대상 안 정류소(원천 이름·좌표), `is_virtual=1` 은 타고 내릴 수 없는 통과 노드다.
`route_type` 은 노선 유형을 카카오 버스 유형 이름(간선·지선·순환·광역·직행·일반·마을·시외·공항 — 웹 `BUS_COLOR` 의 키)에 맞춘 값이고,
맞는 카카오 유형이 없으면 원천의 짧은 이름(좌석·따복·수요응답·관광·심야)이다. `route_type_src` 는 원천 유형 이름 그대로다
(경기: GBIS 노선유형코드의 이름, 서울: 노선정보조회 routeType 의 이름). 서울 노선 목록에 없는 노선(`청와대A01`)은 빈칸이다.
경기 파일은 날마다 새 버전이 나온다 — `fetch_gbis.py` 를 다시 돌리면 새 버전 폴더에 받고, 정류소 정제와 순서표는 가장 최근 버전을 읽는다.

**배차간격**(`headway_bus.csv` · `headway_rail.csv`): 대기시간 모델의 입력이다.
버스는 원천의 배차값을 그대로 쓴다 — 경기 GBIS 노선 파일이 요일 유형 4개(평일·토요일·일요일·공휴일)의 **최소·최대 배차**와
방향별 첫·막차를, 서울 노선 목록이 `term`(대표 배차 하나, 요일 구분 없음)과 첫·막차를 준다. 경기 원천의 `peekAlloc`·`npeekAlloc`
은 이름과 달리 첨두/비첨두가 아니라 최소·최대다. 0 과 빈값은 정보 없음이다.
도시철도는 KTDB 대중교통 GTFS(`csv/대중교통GTFS(...)/`, 2025년 3월 **평일 1일**)의 실제 시각표에서 역·방향·시간대별로
정차 횟수를 세어 배차(= 180분 / 앞뒤 1시간을 합친 3시간 창의 횟수 `n_trips_3h`)를 만든다 — 한 시간 칸만 세면 정차 1회인 칸이
시 경계에 걸린 것만으로 60분이 되기 때문이다. 급행·지선·순환은 GTFS 가 노선을 따로 두므로 한 역에 서는 모든 패턴이 함께 세어진다.
GTFS 의 **버스** 시각표는 첫차~막차를 대표 배차로 균등 분배한 합성값이라 쓰지 않는다. GTFS 노선명 ↔ 노선군은
`ref/line_groups.csv` 의 `gtfs_names`, 개명으로 이름이 다른 역은 `ref/gtfs_station_map.csv` 로 잇는다.

**실시간 역명**(`subway_live_stations.csv`): 실시간 지하철 도착 조회의 질의 키다 — 그 API 는 역명 **정확 일치**만 받으므로
우리 표시 이름으로 물으면 조용히 0 건이 온다(`양재(서초구청)`→`양재`, `서울역`→`서울`, `○○역` 접미 전부 — 표기가 다른 행이
325개다). 그래서 역마다 질의에 쓸 실시간 원문 이름(`live_name`)과 응답을 거를 키(`live_statn_id`)를 미리 이어 둔다.
매칭은 실시간 역정보 파일의 (노선군, 이름 키) → 별칭 순이고, 파일의 `호선이름` 이 우리 `line_group` 과 같은 문자열이라
노선군 참조표가 따로 필요 없다(`SUBWAY_ID` 도 파일에서 끌어온다). 개명·방향 표기로 이름이 아예 다른 역은
`ref/subway_live_names.csv` 로 잇는다 — 그 표를 자동 매칭보다 **먼저** 적용하고(원천에 살아 있는 옛 이름 `신길온천` 이
엉뚱한 행에 붙지 않게), 대상 역이 역 DB 에 없거나 이름이 다르거나 이미 자동으로 맞으면 멈춘다.
685역 중 642역이 조회 가능하다. 진접선 3역은 열차 목적지(`진접행`)로만 나오고 역별 실시간이 없으며,
용인에버라인·의정부경전철·김포골드라인 40역은 노선군 자체가 실시간에 없다 — 재시도 대상이 아니라 '실시간 미제공' 이다.
조회는 `live_name` 단위로 묶는다(642행의 유일한 이름이 512개다 — 환승역은 한 응답에 여러 노선 행이 섞여 온다).

**역 순서표**(`subway_line_seq.csv`): 한 행 = 순서 안의 역 하나. 순서(`chain_id`) 하나가 운영 노선 또는 지선 하나이고
(`seoul2:성수지선` 처럼 지선은 분기역부터 시작), `loop=1` 이면 끝 역 다음이 첫 역이다. 같은 물리 역은 `phys_id` 가 같아
노선군 안의 순서끼리 이것으로 이어진다. 권역(서울·경기) 밖 역은 뺐고, 권역 안인데 역 DB 에 없는 역은 `status=missing`
(이름만). 원천 `정거장구성` 이 오래됐거나 틀린 곳은 `ref/subway_seq_fixes.csv` 에 한 줄씩 적는다 — 대상 역이 목록에 없으면
스크립트가 멈춘다(원천이 바뀐 신호). 역 DB 에도 원천 역 파일에도 없는 이름이 나오면 멈추므로 `rename` 줄을 더한다.
각 스크립트는 단계별 건수표를 출력하고 같은 표를 `reports/` 에 쓴다 (입력 파일 크기·SHA-256 앞 12자리 포함).
경계 스크립트는 피처 수(행정동 3,558 / 서울 427·경기 602 → 서울 25구·경기 31시군)가 다르면 멈춘다.

## 출처·라이선스

`csv/` 는 저장소에 없다 — 아래 원본을 받아 표의 파일 이름 그대로 `data/csv/` 에 둔다 (스크립트 기본 입력 경로).
`external/` 의 원본은 스크립트가 받는다.

| 데이터 | 출처 | 파일 | 조건 |
|---|---|---|---|
| 버스 정류장 | 국토교통부_전국 버스정류장 위치정보 — 공공데이터포털(data.go.kr) 15067528 | `csv/국토교통부_전국 버스정류장 위치정보_20251031.csv` | 데이터 페이지의 이용허락범위 |
| 서울 버스 정류소 | 서울 열린데이터광장 [OA-15067](https://data.seoul.go.kr/dataList/OA-15067/S/1/datasetView.do) 서울시 버스정류소 위치정보 | `csv/서울시버스정류소위치정보(20260902).xlsx` | 공공누리 1유형 — 출처 표시 |
| 경기 버스 정류소 | 경기도 버스정보 기반정보 — 공공데이터포털(data.go.kr) 15080658 (`fetch_gbis.py`) | `external/gbis/<버전>/station<버전>V2.txt` | 이용허락범위 제한 없음 |
| 도시철도역 | 전국도시철도역사정보표준데이터 — 공공데이터포털(data.go.kr) 15013205 | `csv/전체_도시철도역사정보_20260630.xlsx` | 데이터 페이지의 이용허락범위 |
| 도시철도 노선 | 전국도시철도노선정보표준데이터 — 공공데이터포털(data.go.kr) | `csv/전체_도시철도노선정보_20260630.xlsx` (시트 `표준데이터 노선(전체)`) | 데이터 페이지의 이용허락범위 |
| 서울 버스 노선별 정류소 | 서울 열린데이터광장 [OA-1095](https://data.seoul.go.kr/dataList/OA-1095/F/1/datasetView.do) 서울시 버스노선별 정류소 정보 | `csv/서울시버스노선별정류소정보(20260902).xlsx` | 공공누리 1유형 — 출처 표시 |
| 서울 버스 노선 목록(유형) | 서울특별시_노선정보조회 서비스 — 공공데이터포털(data.go.kr) [15000193](https://www.data.go.kr/data/15000193/openapi.do) (`fetch_seoul_routes.py`, 활용신청 필요) | `external/seoul_bus/<날짜>/busRouteList.csv` | 데이터 페이지의 이용허락범위 |
| 경기 버스 노선–정류소 · 노선(유형) | 경기도 버스정보 기반정보 — 공공데이터포털(data.go.kr) 15080658 (`fetch_gbis.py`) | `external/gbis/<버전>/routestation<버전>V2.txt` · `route<버전>V2.txt` | 이용허락범위 제한 없음 |
| 대중교통 시각표(GTFS) | KTDB 국가교통DB 대중교통 GTFS 기반정보 (2025년 3월 평일 1일, 자료신청) | `csv/대중교통GTFS(2025년 기준)/202503_GTFS_DataSet/` | 자료 제공 조건 |
| 서울 지하철 실시간 도착 역정보 | 서울 열린데이터광장 [OA-12764](https://data.seoul.go.kr/dataList/OA-12764/F/1/datasetView.do) 지하철 실시간 도착정보의 역 목록 | `csv/실시간도착_역정보(20260902).xlsx` | 공공누리 1유형 — 출처 표시 |
| 행정경계 | [vuski/admdongkor](https://github.com/vuski/admdongkor) `ver20260701` (원천 통계청 SGIS) | `external/admdongkor/HangJeongDong_ver20260701.geojson` | CC BY 4.0 — **출처 표기 의무** |

행정경계를 보여 주는 지도·보고서에는 다음을 표기한다.

> 행정경계: 통계청 SGIS(공공누리 1유형), 가공 vuski/admdongkor (CC BY 4.0)

카카오 API 응답은 약관상 저장할 수 없다 — 이 폴더 어디에도 두지 않는다.
