import pytest

from algo.compare import saves_enough


@pytest.mark.parametrize("saved, walking, expected", [
    (-1, True, False), (0, True, False), (1, True, True),
    (299, False, False), (300, False, True),
])
def test_saving_threshold(saved, walking, expected):
    assert saves_enough(1800 - saved, 1800, walking=walking) is expected
