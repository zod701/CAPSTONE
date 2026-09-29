from copy import deepcopy

from app.route_identity import duplicate_walk_route


def test_walk_duplicate_preserves_original_and_ignores_estimates():
    step = {"type": "BUS", "route_ids": ["b", "a"], "stops": ["A", "B"],
            "path": [[127, 37], [127.1, 37.1]], "time_s": 100}
    original = {"steps": [step]}
    row = {"walking": {"path": [[126.9, 36.9], [127, 37]]}, "transit": deepcopy(original)}
    row["transit"]["steps"][0].update(time_s=80, route_ids=["a", "b"])
    assert duplicate_walk_route(row, [original], (126.9, 36.9), (127.2, 37.2))
    assert original["steps"][0]["time_s"] == 100
    for field, value in [("route_ids", ["other"]), ("stops", ["B", "A"]),
                         ("path", [[127, 37], [127.05, 37], [127.1, 37.1]])]:
        changed = deepcopy(row)
        changed["transit"]["steps"][0][field] = value
        assert not duplicate_walk_route(changed, [original], (126.9, 36.9), (127.2, 37.2))
    row["walking"]["path"][1] = [127.01, 37]
    assert not duplicate_walk_route(row, [original], (126.9, 36.9), (127.2, 37.2))


def test_taxi_and_unidentified_routes_are_not_removed():
    assert not duplicate_walk_route({"taxi": {}}, [], (0, 0), (1, 1))
    assert not duplicate_walk_route({"walking": {"path": [[0, 0], [1, 1]]},
                                     "transit": {"steps": [{"type": "BUS"}]}},
                                    [{"steps": [{"type": "BUS"}]}], (0, 0), (1, 1))


def test_transfer_walk_duplicate_and_different_alighting():
    bus = {"type": "BUS", "route_ids": ["a"], "stops": ["A", "B"],
           "path": [[127, 37], [127.1, 37.1]]}
    rail = {"type": "SUBWAY", "line_group": "2호선", "stops": ["C", "D"],
            "path": [[127.2, 37.2], [127.3, 37.3]]}
    original = {"steps": [bus, {"type": "WALKING", "time_s": 300}, rail]}
    row = {"walking": {"path": [[127.1, 37.1], [127.2, 37.2]]},
           "transit": {"steps": [deepcopy(bus), deepcopy(rail)]}}
    assert duplicate_walk_route(row, [original], (126.9, 36.9), (127.4, 37.4))
    row["transit"]["steps"][1]["stops"] = ["C", "E"]
    assert not duplicate_walk_route(row, [original], (126.9, 36.9), (127.4, 37.4))
