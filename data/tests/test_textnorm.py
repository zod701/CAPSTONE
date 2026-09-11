import pytest

from textnorm import is_virtual, name_key, name_norm


def test_whitespace_removed():
    assert name_norm("  판교역 동편 ") == "판교역동편"


@pytest.mark.parametrize("sep", ["·", "∙", "ㆍ", ",", "•", "・", "、"])
def test_separators_unified(sep):
    assert name_norm(f"하남시청(덕풍{sep}신장)") == "하남시청(덕풍.신장)"
    assert name_key(f"A{sep}B", "bus") == "ab"


def test_fullwidth_and_enclosed():
    assert name_norm("（주）판교") == "(주)판교"
    assert name_norm("㈜판교") == "(주)판교"
    assert name_norm("[판교]역") == "(판교)역"


def test_parenthetical_removed_in_key():
    assert name_key("천호(풍납토성)", "subway") == "천호"
    assert name_key("종로2가(중)", "bus") == "종로2가"
    assert name_key("간선(종로2가(중))", "bus") == "간선"


def test_subway_strips_trailing_station_suffix_only():
    assert name_key("주안역", "subway") == name_key("주안", "subway") == "주안"
    assert name_key("판교역", "bus") == "판교역"
    # 두 글자 이름의 "역"은 떼지 않는다 (이름 자체가 사라짐)
    assert name_key("역", "subway") == "역"


def test_dot_inside_name():
    assert name_key("4.19민주묘지", "bus") == name_key("4·19민주묘지", "bus") == "419민주묘지"


def test_kakao_and_standard_forms_meet():
    assert name_key("판교(판교테크노밸리)", "subway") == name_key("판교", "subway")
    assert name_key("미금(분당서울대병원)", "subway") == "미금"


def test_latin_lowercased():
    assert name_key("LH수서아파트", "bus") == "lh수서아파트"


def test_bad_kind():
    with pytest.raises(ValueError):
        name_key("x", "train")


def test_empty_and_none():
    assert name_norm(None) == ""
    assert name_key("", "bus") == ""


def test_is_virtual():
    assert is_virtual("광산IC(미정차)")
    assert is_virtual("노오지JC(가상)")
    assert not is_virtual("판교역")
    assert not is_virtual("가상리")
    assert not is_virtual(None)
