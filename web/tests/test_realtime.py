"""실시간 도착정보 클라이언트 — 응답 본문은 모두 문서 스키마로 손으로 쓴 합성 데이터(실제 API·.env 미사용)."""
import asyncio
import json
import logging
from datetime import datetime
from urllib.parse import unquote

import httpx
import pytest

from app.errors import ApiError
from app.quota import QuotaGuard
from app.realtime import (GG_STOP_URL, SEOUL_STOP_URL, SUBWAY_PAGE, KIND_GG, KIND_SEOUL, KIND_SUBWAY,
                          RealtimeClient, TtlCache, bus_gyeonggi, bus_seoul, line_only, subway)

DKEY = "fakedatagokrkey0123456789abcdefghij0123456789abcdefghij01234567"  # 가짜 키 (64자 영숫자)
SKEY = "fakeseoulsubwaylivekey1234567"                                    # 가짜 키 (30자)
NOTE = "SYNTHETIC - hand-written from documented schema; not a real API response"
LIMITS = {KIND_GG: 1000, KIND_SEOUL: 1000, KIND_SUBWAY: 1000}

# --- 합성 응답 ---


def gg_body(rows, code=0, message="정상적으로 처리되었습니다."):
    head = {"queryTime": "2026-09-12 13:30:09.952", "resultCode": code, "resultMessage": message}
    body = {"comMsgHeader": "", "msgHeader": head}
    if code == 0:
        body["msgBody"] = {"busArrivalList": rows}   # resultCode 4 는 msgBody 키가 아예 없다 (실측)
    return {"_note": NOTE, "response": body}


def gg_row(**kw):
    """값이 없는 실시간 필드는 빈 문자열로 온다 — 도착정보 없는 노선의 기본 모양."""
    return {"routeId": 234001410, "routeName": 925, "routeTypeCd": 13, "staOrder": 63, "stationId": 204000101,
            "flag": "PASS", "routeDestId": 234000980, "routeDestName": "여주역", "turnSeq": 40,
            "predictTime1": "", "predictTime2": "", "locationNo1": "", "locationNo2": "",
            "crowded1": "", "crowded2": "", "remainSeatCnt1": "", "remainSeatCnt2": "",
            "lowPlate1": "", "lowPlate2": "", "plateNo1": "", "plateNo2": "",
            "stationNm1": "", "stationNm2": "", **kw}


GG_ROWS = [
    gg_row(predictTime1=39, locationNo1=12, crowded1=2, remainSeatCnt1=0, lowPlate1=1, plateNo1="경기70아1234",
           crowded2=0),                                 # 값 있는 노선 (crowded2=0 은 미정의라 버린다)
    gg_row(routeId=234001411, routeName="963-1", routeDestName="이천터미널"),   # 도착정보 없는 노선
]

SEOUL_ROWS = [
    {"stId": "100000201", "stNm": "종로2가", "arsId": "01201", "staOrd": "23", "busRouteId": "100100112",
     "rtNm": "721", "routeType": "3", "term": "9", "mkTm": "2026-09-12 13:29:55.0",
     "firstTm": "20260912045700", "lastTm": "20260912222500",
     "arrmsg1": "5분32초후[2번째 전]", "traTime1": "332", "sectOrd1": "21", "isArrive1": "0", "isLast1": "0",
     "busType1": "1", "plainNo1": "서울74사4169", "vehId1": "112029097", "stationNm1": "광화문역",
     "arrmsg2": "12분35초후[7번째 전]", "traTime2": "755", "sectOrd2": "16", "isArrive2": "0", "isLast2": "1"},
    {"stId": "100000201", "rtNm": "143", "busRouteId": "100100016", "staOrd": "92",
     "arrmsg1": "곧 도착", "traTime1": "115", "sectOrd1": "92", "isArrive1": "0", "isLast1": "0",
     "plainNo1": "서울74사1143", "arrmsg2": "5분후[3번째 전]", "traTime2": "300", "isLast2": "0"},
    {"stId": "100000201", "rtNm": "1102", "busRouteId": "100100600", "staOrd": "10",
     "arrmsg1": "운행종료", "traTime1": "0", "sectOrd1": "0", "isLast1": "0", "plainNo1": " ",
     "arrmsg2": "출발대기", "traTime2": "0", "sectOrd2": "0"},
]


def seoul_body(rows, code="0", message="정상적으로 처리되었습니다."):
    # itemCount 는 실측 25건에도 0 으로 왔다 — 일부러 0 으로 두어 회귀를 막는다
    return {"_note": NOTE, "msgHeader": {"headerMsg": message, "headerCd": code, "itemCount": 0},
            "msgBody": {"itemList": rows}}


SUBWAY_ROWS = [
    {"subwayId": "1032", "statnNm": "정자", "updnLine": "상행", "trainLineNm": "운정중앙행 - 판교방면",
     "barvlDt": "240", "arvlCd": "99", "arvlMsg2": "[2]번째 전역 (판교)", "arvlMsg3": "판교",
     "btrainSttus": "일반", "btrainNo": "1120", "bstatnNm": "운정중앙", "lstcarAt": "0",
     "recptnDt": "2026-09-12 13:22:27", "ordkey": "01000운정중앙0"},
    {"subwayId": "1032", "statnNm": "정자", "updnLine": "하행", "trainLineNm": "동탄행 - 미금방면",
     "barvlDt": "0", "arvlCd": "0", "arvlMsg2": "정자역 진입", "btrainSttus": "급행",
     "btrainNo": "1121", "bstatnNm": "동탄", "lstcarAt": "1", "recptnDt": "2026-09-12 13:29:19"},
    {"subwayId": "1075", "statnNm": "정자", "updnLine": "하행", "trainLineNm": "수원행 - 오리방면",
     "barvlDt": "0", "arvlCd": "2", "arvlMsg2": "정자역 출발", "btrainSttus": "일반",
     "btrainNo": "K3080", "bstatnNm": "수원", "lstcarAt": "0", "recptnDt": "2026-09-12 13:29:40"},
]


def subway_body(rows, code="INFO-000", message="정상 처리되었습니다."):
    return {"_note": NOTE, "errorMessage": {"status": 200, "code": code, "message": message, "link": "",
                                            "developerMessage": "", "total": len(rows)},
            "realtimeArrivalList": rows}


SUBWAY_EMPTY = {"_note": NOTE, "status": 500, "code": "INFO-200", "message": "해당하는 데이터가 없습니다.",
                "link": "", "developerMessage": "", "total": 0}   # 없는 역명: errorMessage 래퍼 없이 루트로

GATEWAY_XML = ('<OpenAPI_ServiceResponse><cmmMsgHeader><returnAuthMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR'
               '</returnAuthMsg><returnReasonCode>{code}</returnReasonCode></cmmMsgHeader>'
               '</OpenAPI_ServiceResponse>')

# --- 도우미 ---


class Upstream:
    """MockTransport 핸들러: 요청을 기록하고 정해진 응답 또는 예외를 돌려준다."""

    def __init__(self, status=200, json_body=None, text=None, exc=None):
        self.status, self.json_body, self.text, self.exc = status, json_body, text, exc
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        if self.exc is not None:
            raise self.exc
        if self.text is not None:
            return httpx.Response(self.status, text=self.text)
        return httpx.Response(self.status, json=self.json_body)


class Clock:
    """주입하는 단조 시계 — TTL 을 테스트에서 넘긴다."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


# 도시철도 예측은 수신 시각(recptnDt) 기준이라 벽시계를 고정해야 결과가 정해진다 — 픽스처 첫 행 수신 20초 뒤
NOW = datetime(2026, 9, 12, 13, 22, 47).timestamp()


def make(tmp_path, upstream, *, key=DKEY, subway_key=SKEY, limits=None, ttl_s=15.0, clock=None, now=None):
    quota = QuotaGuard(tmp_path / "quota.json", limits or LIMITS)
    http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    client = RealtimeClient(http, quota, data_go_kr_key=key, subway_key=subway_key,
                            timeout_s=5.0, ttl_s=ttl_s, clock=clock or Clock(), now=now or (lambda: NOW))
    return client, quota


def used(quota, kind):
    return quota.snapshot()[kind]["used"]


def raised(coro_fn):
    """ApiError 를 돌려준다 — 봉투에 키가 없는지 함께 확인한다."""
    with pytest.raises(ApiError) as ei:
        asyncio.run(coro_fn())
    err = ei.value
    body = json.dumps(err.to_body(), ensure_ascii=False)
    assert DKEY not in body and SKEY not in body
    return err


# --- 경기 GBIS ---

def test_gyeonggi_normal(tmp_path):
    up = Upstream(200, gg_body(GG_ROWS))
    client, quota = make(tmp_path, up)
    items = asyncio.run(client.gyeonggi_stop("204000101"))
    assert items == [
        {"source": "gyeonggi", "route_id": "234001410", "route_name": "925", "route_type": None,
         "eta_s": 39 * 60, "n_stops_ahead": 12, "crowding": 2, "seats": None, "is_last": None,
         "vehicle": "경기70아1234", "message": None, "dest": "여주역"},
        {"source": "gyeonggi", "route_id": "234001411", "route_name": "963-1", "route_type": None,
         "eta_s": None, "n_stops_ahead": None, "crowding": None, "seats": None, "is_last": None,
         "vehicle": None, "message": "도착 정보가 없습니다.", "dest": "이천터미널"},
    ]
    assert used(quota, KIND_GG) == 1 and used(quota, KIND_SEOUL) == 0
    req = up.requests[0]
    assert str(req.url).startswith(GG_STOP_URL + "?")
    assert dict(req.url.params) == {"format": "json", "stationId": "204000101", "serviceKey": DKEY}


def test_gyeonggi_service_key_is_unquoted(tmp_path):
    # .env 에 인코딩된 키가 들어 있으면 httpx 가 다시 인코딩한다 → 보내기 전에 한 번 풀어야 한다
    enc = "abc%3Ddef" + DKEY
    client, _ = make(tmp_path, up := Upstream(200, gg_body([])), key=enc)
    asyncio.run(client.gyeonggi_stop("204000101"))
    assert dict(up.requests[0].url.params)["serviceKey"] == unquote(enc) == "abc=def" + DKEY


def test_gyeonggi_second_vehicle_and_predict_time_sec():
    # 단건 API 만 predictTimeSec1 을 준다 — 있으면 초 단위를 쓴다 (39분 ↔ 2346초)
    rows = [gg_row(predictTime1=39, predictTimeSec1=2346, predictTime2=51, locationNo2=17, crowded2=3,
                   plateNo2="경기70아5678")]
    items = bus_gyeonggi(gg_body(rows))
    assert [(i["eta_s"], i["n_stops_ahead"], i["crowding"], i["vehicle"]) for i in items] == [
        (2346, None, None, None), (51 * 60, 17, 3, "경기70아5678")]


def test_gyeonggi_empty_result_is_not_an_error(tmp_path):
    # 없는 정류소(실측): resultCode 4, msgBody 키 없음
    client, quota = make(tmp_path, Upstream(200, gg_body(None, code=4, message="결과가 존재하지 않습니다.")))
    assert asyncio.run(client.gyeonggi_stop("999999999")) == []
    assert used(quota, KIND_GG) == 1        # 원천을 불렀으니 한 건이다


def test_gyeonggi_body_error(tmp_path):
    client, quota = make(tmp_path, Upstream(200, gg_body(None, code=99, message="서비스 점검 중입니다.")))
    err = raised(lambda: client.gyeonggi_stop("204000101"))
    assert (err.code, err.status) == ("upstream_error", 502)
    assert err.message == "경기 버스 도착정보 요청이 실패했습니다: 서비스 점검 중입니다."
    assert used(quota, KIND_GG) == 1        # 원천에 닿았으므로 예약을 그대로 둔다


def test_gyeonggi_body_error_masks_echoed_key(tmp_path):
    client, _ = make(tmp_path, Upstream(200, gg_body(None, code=99, message=f"bad key {DKEY}")))
    err = raised(lambda: client.gyeonggi_stop("204000101"))
    assert DKEY not in err.message and "<REDACTED>" in err.message


@pytest.mark.parametrize("code, expect, left", [
    ("30", "arrivals_auth", 0),        # 등록되지 않은 키 — 원천 쿼터를 쓰지 않았다
    ("22", "quota_exceeded", 1000),    # 일 트래픽 초과 — 그날 남은 호출을 0 으로
    ("99", "upstream_error", 1),       # 그 밖의 거절 — 예약은 그대로
])
def test_gyeonggi_gateway_rejection_is_xml(tmp_path, code, expect, left):
    up = Upstream(200, text=GATEWAY_XML.format(code=code))
    client, quota = make(tmp_path, up)
    err = raised(lambda: client.gyeonggi_stop("204000101"))
    assert err.code == expect
    assert "SERVICE_KEY_IS_NOT_REGISTERED_ERROR" not in err.message   # 본문을 문구에 넣지 않는다
    assert used(quota, KIND_GG) == left
    if expect == "arrivals_auth":
        assert "DATA_GO_KR_API_KEY" in err.action and err.status == 503


# --- 서울 버스 ---

def test_seoul_normal(tmp_path):
    up = Upstream(200, seoul_body(SEOUL_ROWS))
    client, quota = make(tmp_path, up)
    items = asyncio.run(client.seoul_stop("100000201"))
    # itemCount 가 0 이어도 itemList 를 그대로 센다 (실측 회귀)
    assert [(i["route_name"], i["eta_s"], i["n_stops_ahead"], i["is_last"], i["message"]) for i in items] == [
        ("721", 332, 2, False, "5분32초후[2번째 전]"),
        ("721", 755, 7, True, "12분35초후[7번째 전]"),
        ("143", 115, None, False, "곧 도착"),
        ("143", 300, 3, False, "5분후[3번째 전]"),          # 초가 0 이면 '초' 가 빠진다
        ("1102", None, None, False, "운행종료"),            # traTime "0" = 예측 없음
    ]
    assert items[0] == {"source": "seoul", "route_id": "100100112", "route_name": "721", "route_type": None,
                        "eta_s": 332, "n_stops_ahead": 2, "crowding": None, "seats": None, "is_last": False,
                        "vehicle": "서울74사4169", "message": "5분32초후[2번째 전]", "dest": None}
    assert used(quota, KIND_SEOUL) == 1
    req = up.requests[0]
    assert str(req.url).startswith(SEOUL_STOP_URL + "?")
    # 서울은 .env 원문을 그대로 넘겨야 통했다 (경기와 반대)
    assert dict(req.url.params) == {"stId": "100000201", "resultType": "json", "serviceKey": DKEY}


def test_seoul_service_key_is_sent_raw(tmp_path):
    enc = "abc%3Ddef" + DKEY
    client, _ = make(tmp_path, up := Upstream(200, seoul_body([])), key=enc)
    asyncio.run(client.seoul_stop("100000201"))
    assert dict(up.requests[0].url.params)["serviceKey"] == enc


@pytest.mark.parametrize("rows", [None, []])
def test_seoul_empty_item_list(rows):
    assert bus_seoul(seoul_body(rows)) == []


def test_seoul_body_error(tmp_path):
    client, quota = make(tmp_path, Upstream(200, seoul_body(None, code="4", message="인증에 실패하였습니다.")))
    err = raised(lambda: client.seoul_stop("100000201"))
    assert (err.code, err.status) == ("upstream_error", 502)
    assert err.message == "서울 버스 도착정보 요청이 실패했습니다: 인증에 실패하였습니다."
    assert used(quota, KIND_SEOUL) == 1


def test_seoul_401_is_auth_and_not_counted(tmp_path):
    up = Upstream(401, {"error": {"status": 401, "message": f"권한이 없습니다 ({DKEY})"}})
    client, quota = make(tmp_path, up)
    err = raised(lambda: client.seoul_stop("100000201"))
    assert (err.code, err.status, err.upstream_status) == ("arrivals_auth", 503, 401)
    assert "DATA_GO_KR_API_KEY" in err.action and "data.go.kr" in err.action
    assert used(quota, KIND_SEOUL) == 0     # 원천 쿼터를 쓰지 않은 거절 → 되돌린다


def test_upstream_429_marks_exhausted(tmp_path):
    up = Upstream(429, {"error": "too many requests"})
    client, quota = make(tmp_path, up, limits={**LIMITS, KIND_SEOUL: 5})
    err = raised(lambda: client.seoul_stop("100000201"))
    assert (err.code, err.status, err.upstream_status) == ("quota_exceeded", 429, 429)
    assert "서울 버스 도착정보" in err.message and "(5건)" in err.message
    assert quota.snapshot()[KIND_SEOUL] == {"used": 5, "limit": 5, "remaining": 0}
    assert raised(lambda: client.seoul_stop("100000201")).code == "quota_exceeded"
    assert len(up.requests) == 1            # 두 번째는 원천을 부르지 않는다


def test_non_200_is_upstream_error(tmp_path):
    client, quota = make(tmp_path, Upstream(503, text="<html>Service Unavailable</html>"))
    err = raised(lambda: client.seoul_stop("100000201"))
    assert (err.code, err.status, err.upstream_status) == ("upstream_error", 502, 503)
    assert err.message == "서울 버스 도착정보 요청이 실패했습니다 (HTTP 503)."
    assert "Service Unavailable" not in err.message
    assert used(quota, KIND_SEOUL) == 1


# --- 서울 도시철도 ---

def test_subway_filters_by_line(tmp_path):
    up = Upstream(200, subway_body(SUBWAY_ROWS))
    client, quota = make(tmp_path, up)
    items = asyncio.run(client.subway_station("정자", "1032"))
    assert items == [
        # 예측 240초에서 수신 뒤 지난 20초를 뺀다
        {"direction": "상행", "eta_s": 220, "age_s": 20, "n_stops_ahead": 2, "message": "[2]번째 전역 (판교)",
         "train_type": "일반", "dest": "운정중앙", "headsign": "운정중앙행 - 판교방면", "train_no": "1120"},
        # barvlDt=0 이지만 arvlCd=0(진입) 이라 정말 지금이다. 수신 시각이 우리 시계보다 앞서면 경과는 0
        {"direction": "하행", "eta_s": 0, "age_s": 0, "n_stops_ahead": None, "message": "정자역 진입",
         "train_type": "급행", "dest": "동탄", "headsign": "동탄행 - 미금방면", "train_no": "1121"},
    ]
    # 환승역은 한 응답에 여러 노선이 섞여 온다 — 다른 노선은 같은 응답을 걸러 쓴다 (쿼터 1)
    other = asyncio.run(client.subway_station("정자", "1075"))
    # 출발(arvlCd=2)은 이 역을 떠난 열차다 — barvlDt=0 을 0초로 담으면 '지금 도착'으로 오해된다
    assert [(i["dest"], i["message"], i["eta_s"]) for i in other] == [("수원", "정자역 출발", None)]
    assert len(up.requests) == 1 and used(quota, KIND_SUBWAY) == 1
    # 순수 함수는 역 전체를 주고 노선 필터용 line_id 를 함께 담는다 (응답 모양에는 나가지 않는다)
    rows = subway(subway_body(SUBWAY_ROWS), NOW)
    assert [r["line_id"] for r in rows] == ["1032", "1032", "1075"]
    assert line_only(rows) == items + other                 # 노선을 주지 않으면 전부
    assert line_only(rows, "1032") == items and line_only(rows, "1075") == other


@pytest.mark.parametrize("barvl, code, recptn, eta, age", [
    ("240", "99", "2026-09-12 13:22:27", 220, 20),     # 예측에서 수신 뒤 지난 시간을 뺀다
    ("240", "99", "2026-09-12 13:18:00", None, 287),   # 예측 시각이 이미 지났다 → 모름(0 이라고 우기지 않는다)
    ("0", "0", "2026-09-12 13:22:40", 0, 7),           # 진입 — 정말 지금
    ("0", "1", "2026-09-12 13:22:40", 0, 7),           # 도착 — 정말 지금
    ("0", "2", "2026-09-12 13:22:40", None, 7),        # 출발 — 이 역을 떠난 열차다
    ("0", "99", "2026-09-12 13:22:40", None, 7),       # 운행중이지만 예측이 없다 → 모름
    ("0", "99", "2026-09-12 13:22:40", None, 7),       # (20역 떨어진 열차가 여기 들어온다)
    ("60", "1", "2026-09-12 13:25:00", 60, 0),         # 수신 시각이 우리 시계보다 앞선다(실측) → 경과 0
    ("", "99", "", None, None),                        # 값이 비어도 죽지 않는다
])
def test_subway_eta_rules(barvl, code, recptn, eta, age):
    """`eta_s` 는 '기다릴 초'다 — barvlDt 를 그대로 담으면 낡은 예측과 떠난 열차가 모두 '지금'이 된다."""
    row = {"subwayId": "1075", "updnLine": "상행", "barvlDt": barvl, "arvlCd": code,
           "arvlMsg2": "[20]번째 전역 (수원)", "recptnDt": recptn}
    item = subway(subway_body([row]), NOW)[0]
    assert (item["eta_s"], item["age_s"]) == (eta, age)
    assert item["n_stops_ahead"] == 20      # 예측이 없을 때 임박 여부를 가릴 유일한 값


def test_subway_station_name_is_path_segment(tmp_path):
    up = Upstream(200, subway_body([]))
    client, _ = make(tmp_path, up)
    asyncio.run(client.subway_station("신길온천", "1075"))   # DB 이름 '능길' → 실시간 이름 (실측 사례)
    req = up.requests[0]
    # 페이지 크기는 넉넉히 — 20 이면 노선 5개가 걸친 서울역(total=22)에서 뒤 노선이 통째로 잘린다
    assert unquote(req.url.path).endswith(f"/realtimeStationArrival/0/{SUBWAY_PAGE}/신길온천")
    assert SUBWAY_PAGE >= 60
    assert SKEY in unquote(req.url.path) and DKEY not in str(req.url)   # 지하철은 전용 키


@pytest.mark.parametrize("body", [SUBWAY_EMPTY, {"errorMessage": {"code": "INFO-000", "total": 0}}])
def test_subway_empty_is_not_an_error(tmp_path, body):
    # 없는 역명(루트에 INFO-200) · 목록 키가 아예 없는 응답 둘 다 빈 결과다
    client, quota = make(tmp_path, Upstream(200, body))
    assert asyncio.run(client.subway_station("없는역이름", "1032")) == []
    assert used(quota, KIND_SUBWAY) == 1


def test_subway_body_error(tmp_path):
    client, quota = make(tmp_path, Upstream(200, subway_body([], code="INFO-100", message="인증키가 유효하지 않습니다.")))
    err = raised(lambda: client.subway_station("정자", "1032"))
    assert (err.code, err.status) == ("upstream_error", 502)
    assert err.message == "서울 도시철도 실시간 도착 요청이 실패했습니다: 인증키가 유효하지 않습니다."
    assert used(quota, KIND_SUBWAY) == 1


# --- 캐시 ---

def test_cache_hit_skips_network_and_quota(tmp_path):
    up = Upstream(200, gg_body(GG_ROWS))
    clock = Clock()
    client, quota = make(tmp_path, up, clock=clock, ttl_s=15.0)
    first = asyncio.run(client.gyeonggi_stop("204000101"))
    clock.now = 14.9
    again = asyncio.run(client.gyeonggi_stop("204000101"))
    assert again == first
    assert len(up.requests) == 1 and used(quota, KIND_GG) == 1
    clock.now = 15.0                        # TTL 만료 → 다시 부른다
    asyncio.run(client.gyeonggi_stop("204000101"))
    assert len(up.requests) == 2 and used(quota, KIND_GG) == 2


def test_cache_keys_are_per_source_and_stop(tmp_path):
    up = Upstream(200, gg_body([]))
    client, _ = make(tmp_path, up)
    asyncio.run(client.gyeonggi_stop("204000101"))
    asyncio.run(client.gyeonggi_stop("204000102"))
    assert len(up.requests) == 2


def test_cache_returns_copies(tmp_path):
    client, _ = make(tmp_path, Upstream(200, gg_body(GG_ROWS)))
    items = asyncio.run(client.gyeonggi_stop("204000101"))
    items[0]["route_type"] = "간선"          # 라우터가 순서표에서 채우는 자리
    assert asyncio.run(client.gyeonggi_stop("204000101"))[0]["route_type"] is None


def test_cache_does_not_keep_errors(tmp_path):
    up = Upstream(200, gg_body(None, code=99, message="점검 중"))
    client, quota = make(tmp_path, up)
    for _ in range(2):
        assert raised(lambda: client.gyeonggi_stop("204000101")).code == "upstream_error"
    assert len(up.requests) == 2 and used(quota, KIND_GG) == 2


def test_ttl_cache_expiry_cleans_up():
    clock = Clock()
    cache = TtlCache(15.0, clock)
    cache.put(("a", "1"), [{"eta_s": 1}])
    clock.now = 20.0
    cache.put(("b", "2"), [])
    assert cache.get(("a", "1")) is None and cache._d.keys() == {("b", "2")}


def test_zero_ttl_always_misses(tmp_path):
    up = Upstream(200, gg_body([]))
    client, quota = make(tmp_path, up, ttl_s=0.0)
    asyncio.run(client.gyeonggi_stop("204000101"))
    asyncio.run(client.gyeonggi_stop("204000101"))
    assert len(up.requests) == 2 and used(quota, KIND_GG) == 2


# --- 쿼터·키·예외 ---

def test_local_limit_blocks_without_call(tmp_path):
    up = Upstream(200, gg_body(GG_ROWS))
    client, quota = make(tmp_path, up, limits={**LIMITS, KIND_GG: 1}, ttl_s=0.0)
    asyncio.run(client.gyeonggi_stop("204000101"))
    err = raised(lambda: client.gyeonggi_stop("204000102"))
    assert (err.code, err.status, err.upstream_status) == ("quota_exceeded", 429, None)
    assert "경기 버스 도착정보" in err.message and "(1건)" in err.message
    assert len(up.requests) == 1
    # 다른 원천은 막히지 않는다
    client2, _ = make(tmp_path, Upstream(200, seoul_body([])), limits={**LIMITS, KIND_GG: 1})
    assert asyncio.run(client2.seoul_stop("100000201")) == []


@pytest.mark.parametrize("call, kind, env", [
    (lambda c: c.gyeonggi_stop("204000101"), KIND_GG, "DATA_GO_KR_API_KEY"),
    (lambda c: c.seoul_stop("100000201"), KIND_SEOUL, "DATA_GO_KR_API_KEY"),
    (lambda c: c.subway_station("정자", "1032"), KIND_SUBWAY, "SEOUL_SUBWAY_LIVE_API"),
])
def test_missing_key_no_call(tmp_path, call, kind, env):
    up = Upstream(200, gg_body([]))
    for empty in (None, ""):
        client, quota = make(tmp_path, up, key=empty, subway_key=empty)
        err = raised(lambda: call(client))
        assert (err.code, err.status) == ("missing_key", 503)
        assert env in err.action
        assert used(quota, kind) == 0
    assert up.requests == []


def test_connect_error_not_recorded_and_no_key_leak(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    up = Upstream(exc=httpx.ConnectError(f"cannot reach {GG_STOP_URL}?serviceKey={DKEY}"))
    client, quota = make(tmp_path, up)
    err = raised(lambda: client.gyeonggi_stop("204000101"))
    assert (err.code, err.status) == ("upstream_unreachable", 502)
    assert err.__suppress_context__          # 연쇄 traceback 에도 예외 문자열을 남기지 않는다
    assert DKEY not in err.message and DKEY not in caplog.text
    assert used(quota, KIND_GG) == 0


def test_timeout_is_recorded(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    client, quota = make(tmp_path, Upstream(exc=httpx.ReadTimeout(f"timed out {SKEY}")))
    err = raised(lambda: client.subway_station("정자", "1032"))
    assert (err.code, err.status) == ("upstream_timeout", 504)
    assert err.message == "서울 도시철도 실시간 도착 응답이 5초 안에 오지 않았습니다."
    assert SKEY not in caplog.text
    assert used(quota, KIND_SUBWAY) == 1     # 원천에 닿았을 수 있다


def test_other_transport_error_is_recorded(tmp_path):
    client, quota = make(tmp_path, Upstream(exc=httpx.ReadError("connection reset")))
    err = raised(lambda: client.seoul_stop("100000201"))
    assert (err.code, err.status) == ("upstream_error", 502)
    assert used(quota, KIND_SEOUL) == 1


def test_nothing_is_logged(tmp_path, caplog):
    """원천 본문·키를 로그에 남기지 않는다 (httpx 요청 로그도 꺼져 있어야 한다)."""
    caplog.set_level(logging.DEBUG)
    for up, call in ((Upstream(200, gg_body(GG_ROWS)), lambda c: c.gyeonggi_stop("204000101")),
                     (Upstream(200, seoul_body(SEOUL_ROWS)), lambda c: c.seoul_stop("100000201")),
                     (Upstream(200, subway_body(SUBWAY_ROWS)), lambda c: c.subway_station("정자", "1032"))):
        client, _ = make(tmp_path, up)
        asyncio.run(call(client))
    for text in (DKEY, SKEY, NOTE, "경기70아1234", "서울74사4169", "운정중앙행 - 판교방면"):
        assert text not in caplog.text
