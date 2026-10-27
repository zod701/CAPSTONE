from algo.compare import saves_enough


def test_saving_threshold():
    assert (saves_enough(1800 - 299, 1800), saves_enough(1800 - 300, 1800)) == (False, True)
