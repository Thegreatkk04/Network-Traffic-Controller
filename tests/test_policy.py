from controller.core.policy import (
    DeviceState, Smoother, ActivityDebouncer, policy_v1, policy_v2, max_min_share,
)

LINK = 10000


def test_max_min_share():
    got = max_min_share({"a": 2, "b": 10, "c": 10}, 12)
    assert got == {"a": 2, "b": 5, "c": 5}


def test_call_protected_downloads_equal():
    devs = [DeviceState("call", "call"), DeviceState("d1", "download"),
            DeviceState("d2", "download")]
    out = policy_v1(devs, LINK)
    assert out["call"].rate_kbps >= 3000
    assert out["d1"].rate_kbps == out["d2"].rate_kbps
    assert sum(a.rate_kbps for a in out.values()) <= LINK


def test_nobody_gets_zero_with_many_devices():
    devs = [DeviceState(f"d{i}", "download") for i in range(30)]
    out = policy_v1(devs, LINK)
    assert all(a.rate_kbps > 0 for a in out.values())
    assert sum(a.rate_kbps for a in out.values()) <= LINK


def test_low_confidence_is_treated_gently():
    devs = [DeviceState("x", "call", confidence=0.4)]
    assert policy_v1(devs, LINK)["x"].priority == 2     # 'unknown' priority


def test_leftover_is_not_wasted():
    devs = [DeviceState("a", "call"), DeviceState("b", "call")]
    out = policy_v1(devs, LINK)
    assert sum(a.rate_kbps for a in out.values()) >= LINK - 2


def test_v2_idle_device_gives_back_bandwidth():
    devs = [DeviceState("call", "call", demand_kbps=0),
            DeviceState("d1", "download", demand_kbps=5000),
            DeviceState("d2", "download", demand_kbps=5000)]
    v1 = policy_v1(devs, LINK)
    v2 = policy_v2(devs, LINK)
    assert v2["call"].rate_kbps == 500                   # only the floor
    assert v2["d1"].rate_kbps > v1["d1"].rate_kbps


def test_v2_active_call_still_protected():
    devs = [DeviceState("call", "call", demand_kbps=1800),
            DeviceState("d1", "download", demand_kbps=8000)]
    out = policy_v2(devs, LINK)
    assert out["call"].rate_kbps >= 1800


def test_smoother_ignores_small_change_and_hold():
    s = Smoother(threshold=0.12, hold_seconds=6)
    first = policy_v1([DeviceState("a", "call")], 10000)
    s.apply(first, now=0)
    small = {"a": type(first["a"])(first["a"].rate_kbps + 10, 10000, 0)}
    assert s.apply(small, now=100)["a"].rate_kbps == first["a"].rate_kbps
    big = {"a": type(first["a"])(first["a"].rate_kbps * 2, 10000, 0)}
    assert s.apply(big, now=2)["a"].rate_kbps == first["a"].rate_kbps   # too soon
    assert s.apply(big, now=100)["a"].rate_kbps == big["a"].rate_kbps


def test_debouncer_needs_three_readings():
    d = ActivityDebouncer(k=3)
    assert d.update("a", "browsing") == "browsing"
    assert d.update("a", "call") == "browsing"
    assert d.update("a", "call") == "browsing"
    assert d.update("a", "call") == "call"