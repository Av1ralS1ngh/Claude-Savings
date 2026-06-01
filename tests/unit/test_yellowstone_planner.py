from decompute_gateway.yellowstone import TrafficProfile, YellowstonePlanner, scan_local_devices


def test_yellowstone_explains_route():
    planner = YellowstonePlanner(scan_local_devices())
    plan = planner.plan("claude-sonnet-4-5", TrafficProfile(raw_input_tokens=10000, yscompress_input_tokens=4000, yellowstone_final_input_tokens=3500))
    assert plan.route
    assert plan.explanation
    assert plan.tokens_saved == 6500
    assert plan.yellowstone_tokens_saved == 500
