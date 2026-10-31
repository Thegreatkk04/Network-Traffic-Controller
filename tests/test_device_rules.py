from controller.core.flow_policy import (DeviceFlows, FlowState, allocate_flows, device_totals,
                                         equal_shares)
from controller.engine import parse_lease, rules_by_ip


def call(ip, kbps=2500):
    return DeviceFlows(ip, [FlowState(f"{ip}-call", "call", 0.95, kbps)])


def download(ip):
    return DeviceFlows(ip, [FlowState(f"{ip}-dl", "download", 0.97, 20000)])


def test_high_tier_call_is_served_before_other_calls():
    devs = [call(f"10.42.0.{i}") for i in range(2, 22)]           # 20 calls, 25.5 Mbps
    t = device_totals(allocate_flows(devs, 25500, "v2", tiers={"10.42.0.2": 0}))
    assert t["10.42.0.2"] >= 3000                                  # VIP gets its full call want
    others = [v for ip, v in t.items() if ip != "10.42.0.2"]
    assert max(others) < t["10.42.0.2"]
    assert 25500 - len(devs) <= sum(t.values()) <= 25500           # rates are truncated to whole kbps


def test_high_tier_download_never_beats_a_normal_call():
    devs = [call("10.42.0.2"), download("10.42.0.3")]
    out = allocate_flows(devs, 3000, "v2", tiers={"10.42.0.3": 0})
    assert out["10.42.0.2"]["10.42.0.2-call"].rate_kbps >= 2000    # the call is still served first


def test_low_tier_goes_last_in_its_class():
    devs = [download("10.42.0.2"), download("10.42.0.3")]
    t = device_totals(allocate_flows(devs, 8000, "v2", tiers={"10.42.0.3": 2}))
    assert t["10.42.0.2"] > t["10.42.0.3"]


def test_cap_limits_device_and_frees_the_rest():
    devs = [download("10.42.0.2"), download("10.42.0.3")]
    t = device_totals(allocate_flows(devs, 8000, "v2", caps={"10.42.0.2": 1000}))
    assert t["10.42.0.2"] <= 1000
    assert t["10.42.0.3"] >= 7000


def test_cap_below_floor():
    devs = [download("10.42.0.2"), download("10.42.0.3")]
    t = device_totals(allocate_flows(devs, 8000, "v1", caps={"10.42.0.2": 200}))
    assert t["10.42.0.2"] == 200


def test_no_rules_keeps_old_behaviour():
    devs = [call("10.42.0.2"), download("10.42.0.3")]
    assert allocate_flows(devs, 8000, "v2") == allocate_flows(devs, 8000, "v2", tiers={}, caps={})


def test_equal_mode_respects_caps():
    s = equal_shares(["a", "b", "c"], 9000, {"a": 1000})
    assert s["a"] == 1000 and s["b"] == s["c"] == 4000


def test_rules_follow_the_mac_to_the_current_ip():
    rules = {"aa:bb:cc:00:00:01": {"priority": "high", "cap_down": 1000},
             "aa:bb:cc:00:00:02": {"blocked": True},
             "aa:bb:cc:00:00:03": {"name": "Papa", "priority": "normal"}}
    macs = {"10.42.0.50": "aa:bb:cc:00:00:01", "10.42.0.60": "aa:bb:cc:00:00:02",
            "10.42.0.70": "aa:bb:cc:00:00:03"}
    tiers, caps, blocked = rules_by_ip(rules, macs)
    assert tiers == {"10.42.0.50": 0}
    assert caps == {"10.42.0.50": (1000, None)}
    assert blocked == {"10.42.0.60"}


def test_parse_lease():
    assert parse_lease("1793360000 AA:BB:CC:DD:EE:FF 10.42.0.88 iPad 01:aa:bb") == \
        ("10.42.0.88", "aa:bb:cc:dd:ee:ff", "iPad")
    assert parse_lease("1793360000 aa:bb:cc:dd:ee:ff 10.42.0.9 * *")[2] == ""
    assert parse_lease("garbage") is None
