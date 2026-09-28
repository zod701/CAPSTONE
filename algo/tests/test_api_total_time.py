from algo.compare import reconstruct


def test_complete_route_preserves_api_edge_walk_residual_and_adds_wait():
    route = {"total_time_s": 1000, "fare": {"value": 1500}, "steps": [
        {"type": "BUS", "time_s": 600, "wait_s": 120},
        {"type": "WALKING", "time_s": 100},
    ]}
    result = reconstruct(route, (127, 37), (127.1, 37.1))
    assert result["time_s"] == 1120
    assert result["wait_s"] == 120
    del route["total_time_s"]
    assert reconstruct(route, (127, 37), (127.1, 37.1))["time_s"] == 820
