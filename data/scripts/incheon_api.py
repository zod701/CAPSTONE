"""인천 버스 API XML 계약 (ETL·웹 공용, 표준 라이브러리만 사용).

출처: data.go.kr 15058487 / 15059084 활용가이드. 키·원문 오류 문구는 예외에 담지 않는다.
"""
import xml.etree.ElementTree as ET

ROUTE_URL = "https://apis.data.go.kr/6280000/busRouteService"
ARRIVAL_URL = "https://apis.data.go.kr/6280000/busArrivalService/getAllRouteBusArrivalList"
ROUTE_TYPES = {
    "1": ("지선", "지선형"), "2": ("간선", "간선형"), "3": ("좌석", "좌석형"),
    "4": ("광역", "광역형"), "5": ("공항", "리무진"), "6": ("마을", "마을버스"),
    "7": ("순환", "순환형"), "8": ("급행간선", "급행간선"), "9": ("지선", "지선(순환)"),
}


class IncheonError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(f"인천 API 오류 ({code})")


def parse_page(body):
    """→ {rows, total, page, size}. 정상 빈 목록과 오류/잘린 XML을 구분한다."""
    try:
        root = ET.fromstring(body)
        code = root.findtext(".//returnReasonCode") or root.findtext(".//resultCode")
        if code is None or not code.strip().isdigit():
            raise IncheonError("invalid_response")
        code = str(int(code))
        if code != "0":
            raise IncheonError(code)
        head = root.find("msgHeader")
        total = int(head.findtext("totalCount"))
        page = int(head.findtext("pageNo"))
        size = int(head.findtext("numOfRows"))
        rows = [{e.tag: (e.text or "").strip() for e in row} for row in root.findall("msgBody/itemList")]
        if total < 0 or page < 1 or size < 1 or (total > (page - 1) * size and not rows):
            raise IncheonError("invalid_response")
        if len(rows) != min(size, max(0, total - (page - 1) * size)):
            raise IncheonError("incomplete_response")
        return {"rows": rows, "total": total, "page": page, "size": size}
    except (ET.ParseError, AttributeError, TypeError, ValueError) as exc:
        if isinstance(exc, IncheonError):
            raise
        raise IncheonError("invalid_response") from None
