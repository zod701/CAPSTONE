"""정류소·역 이름 정규화.

ETL 이 CSV 에 쓰는 `name_key` 와 백엔드가 카카오 응답 이름에서 계산하는 키가 같아야
매칭이 성립한다. 그래서 이 모듈 하나만 두고 양쪽이 가져다 쓴다(표준 라이브러리만 사용).
"""
import re
import unicodedata

# 이름 안에서 구분자로 쓰이는 문자들. NFKC 가 일부(ㆍ, ､)를 다른 코드포인트로 바꾸므로
# 정규화 전후에 두 번 치환한다.
_SEP_CHARS = "·∙ㆍ•‧・,､、"
_SEP = str.maketrans({c: "." for c in _SEP_CHARS})
_PAREN = re.compile(r"\([^()]*\)")

KINDS = ("bus", "subway")


def name_norm(s):
    """표시용 정규화: 전각→반각, 공백 제거, 구분자 통일, 대괄호→소괄호."""
    s = (s or "").translate(_SEP)
    s = unicodedata.normalize("NFKC", s).translate(_SEP)
    s = re.sub(r"\s+", "", s)
    s = s.replace("[", "(").replace("]", ")")
    s = re.sub(r"\.{2,}", ".", s)
    return s.strip(".")


def name_key(s, kind):
    """색인·검색용 키.

    괄호 부기명을 지우고(`천호(풍납토성)`→`천호`, `종로2가(중)`→`종로2가`) 구분자 `.` 를 지운다
    (`4.19민주묘지` == `4·19민주묘지`). 지하철만 끝의 "역"을 뗀다 — 버스 정류소 `판교역` 은
    "판교역"이라는 이름이지 역이 아니다.
    """
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}: {kind!r}")
    k = name_norm(s)
    while True:
        stripped = _PAREN.sub("", k)
        if stripped == k:
            break
        k = stripped
    k = k.replace(".", "").lower()
    if kind == "subway" and len(k) >= 3 and k.endswith("역"):
        k = k[:-1]
    return k


def is_virtual(s):
    """BIS 가 경로 표현용으로 넣은 통과 노드. 실제로 타고 내릴 수 없다.
    경기 BIS 는 `(미정차)`, 서울 노선별 정류소 원천은 `(가상)` 으로 적는다 (지명 `가상리` 와 헷갈리지 않게 괄호째 본다)."""
    s = s or ""
    return "미정차" in s or "(가상)" in s
